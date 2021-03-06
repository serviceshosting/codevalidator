#!/usr/bin/env python
# -*- coding: utf-8 -*-

"""
Simple source code validator with file reformatting option (remove trailing WS, pretty print XML, ..)

written by Henning Jacobs <henning@jacobs1.de>
"""

from __future__ import print_function

try:
    from StringIO import StringIO
    BytesIO = StringIO
except ImportError:
    # Python 3
    from io import StringIO, BytesIO
from collections import defaultdict

from tempfile import NamedTemporaryFile
from xml.etree.ElementTree import ElementTree
from xml.etree.ElementTree import fromstring as xmlfromstring
import argparse
import contextlib
import csv
import fnmatch
import json
import logging
import os
import re
import subprocess
import sys
import tempfile
import shutil

if sys.version_info.major == 2:
    # Pythontidy is only supported on Python2
    from pythontidy import PythonTidy

running_on_py3 = sys.version_info.major == 3


NOT_SPACE = re.compile('[^ ]')

TRAILING_WHITESPACE_CHARS = set([b' ', b'\t'])
INDENTATION = '    '

DEFAULT_CONFIG_PATHS = ['~/.codevalidatorrc', '/etc/codevalidatorrc']

DEFAULT_RULES = [
    'utf8',
    'nobom',
    'notabs',
    'nocr',
    'notrailingws',
]

DEFAULT_CONFIG = {
    'exclude_dirs': ['.svn', '.git'],
    'exclude_files': ['.*.swp'],
    'rules': {
        '*.c': DEFAULT_RULES,
        '*.coffee': DEFAULT_RULES + ['coffeelint'],
        '*.conf': DEFAULT_RULES,
        '*.cpp': DEFAULT_RULES,
        '*.css': DEFAULT_RULES,
        '*.erb': DEFAULT_RULES + ['erb'],
        '*.groovy': DEFAULT_RULES,
        '*.h': DEFAULT_RULES,
        '*.htm': DEFAULT_RULES,
        '*.html': DEFAULT_RULES,
        '*.java': DEFAULT_RULES + ['jalopy'],
        '*.js': DEFAULT_RULES + ['jshint'],
        '*.json': DEFAULT_RULES + ['json'],
        '*.jsp': DEFAULT_RULES,
        '*.less': DEFAULT_RULES,
        '*.md': DEFAULT_RULES,
        '*.php': DEFAULT_RULES + ['phpcs'],
        '*.phtml': DEFAULT_RULES,
        '*.pp': DEFAULT_RULES + ['puppet'],
        '*.properties': DEFAULT_RULES + ['ascii'],
        '*.py': DEFAULT_RULES + ['pyflakes', 'pythontidy'],
        '*.rst': DEFAULT_RULES,
        '*.rb': DEFAULT_RULES + ['ruby', 'rubocop'],
        '* *': ['invalidpath'],
        '*.sh': DEFAULT_RULES,
        '*.sql': DEFAULT_RULES + ['sql_semi_colon'],
        '*.sql_diff': DEFAULT_RULES + ['sql_semi_colon'],
        '*.styl': DEFAULT_RULES,
        '*.txt': DEFAULT_RULES,
        '*.vm': DEFAULT_RULES,
        '*.wsdl': DEFAULT_RULES,
        '*.xml': DEFAULT_RULES + ['xml', 'xmlfmt'],
        '*.yaml': DEFAULT_RULES + ['yaml'],
        '*.yml': DEFAULT_RULES + ['yaml'],
        '*pom.xml': ['pomdesc'],
    },
    'options': {'phpcs': {'standard': 'PSR', 'encoding': 'UTF-8'}, 'pep8': {'max_line_length': 120, 'ignore': 'N806',
                'passes': 5}, 'jalopy': {'classpath': '/opt/jalopy/lib/jalopy-1.9.4.jar:/opt/jalopy/lib/jh.jar'}},
    'dir_rules': {'db_diffs': ['sql_diff_dir', 'sql_diff_sql'], 'database': ['database_dir']},
    'create_backup': True,
    'backup_filename': '.{original}.pre-cvfix',
    'verbose': 0,
    'filter_mode': False,
    'quiet': False,
}

CONFIG = DEFAULT_CONFIG

# base directory where we can find our config folder
# NOTE: to support symlinking codevalidator.py into /usr/local/bin/
# we use realpath to resolve the symlink back to our base directory
BASE_DIR = os.path.dirname(os.path.realpath(__file__))

STDIN_CONTENTS = None


class BaseException(Exception):

    def __init__(self, msg):
        self.msg = msg

    def __str__(self):
        return '%s: %s' % (self.__class__.__name__, self.msg)


class ConfigurationError(BaseException):

    '''missing or incorrect codevalidator configuration'''

    pass


class ExecutionError(BaseException):

    '''error while executing some command'''

    pass


def indent_xml(elem, level=0):
    """xmlindent from http://infix.se/2007/02/06/gentlemen-indent-your-xml"""

    i = '\n' + level * INDENTATION
    if len(elem):
        if not elem.text or not elem.text.strip():
            elem.text = i + INDENTATION
        for e in elem:
            indent_xml(e, level + 1)
            if not e.tail or not e.tail.strip():
                e.tail = i + INDENTATION
        if not e.tail or not e.tail.strip():
            e.tail = i
    else:
        if level and (not elem.tail or not elem.tail.strip()):
            elem.tail = i


def message(msg):
    """simple decorator to attach a error message to a validation function"""

    def wrap(f):
        f.message = msg
        return f

    return wrap


def is_python3(fd):
    '''check first line of file object whether it contains "python3" (shebang)'''

    line = fd.readline()
    fd.seek(0)
    return b'python3' in line


@message('has invalid file path (file name or extension is not allowed)')
def _validate_invalidpath(fd):
    return False


@message('contains tabs')
def _validate_notabs(fd):
    '''
    >>> _validate_notabs(BytesIO(b'foo'))
    True

    >>> _validate_notabs(BytesIO(b'a\\tb'))
    False
    '''
    return b'\t' not in fd.read()


def _fix_notabs(src, dst):
    original = src.read()
    fixed = original.replace(b'\t', b' ' * 4)
    dst.write(fixed.decode())


@message('contains carriage return (CR)')
def _validate_nocr(fd):
    return b'\r' not in fd.read()


def _fix_nocr(src, dst):
    original = src.read()
    fixed = original.replace(b'\r', b'')
    dst.write(fixed.decode())


@message('is not UTF-8 encoded')
def _validate_utf8(fd):
    '''
    >>> _validate_utf8(BytesIO(b'foo'))
    True
    '''
    try:
        fd.read().decode('utf-8')
    except UnicodeDecodeError:
        return False
    return True


@message('is not ASCII encoded')
def _validate_ascii(fd):
    try:
        fd.read().decode('ascii')
    except UnicodeDecodeError:
        return False
    return True


@message('has UTF-8 byte order mark (BOM)')
def _validate_nobom(fd):
    return not fd.read(3).startswith(b'\xef\xbb\xbf')


@message('contains invalid indentation (not 4 spaces)')
def _validate_indent4(fd):
    for line in fd:
        g = NOT_SPACE.search(line)
        if g and g.start(0) % 4 != 0:
            if g.group(0) == '*' and g.start(0) - 1 % 4 == 0:
                # hack to exclude block comments aligned on "*"
                pass
            else:
                return False
    return True


@message('contains lines with trailing whitespace')
def _validate_notrailingws(fd):
    '''
    >>> _validate_notrailingws(BytesIO(b''))
    True

    >>> _validate_notrailingws(BytesIO(b'a '))
    False
    '''
    for line in fd:
        if line.rstrip(b'\n\r')[-1:] in TRAILING_WHITESPACE_CHARS:
            return False
    return True


def _fix_notrailingws(src, dst):
    for line in src:
        dst.write(line.rstrip())
        dst.write('\n')


@message('is not well-formatted (pretty-printed) XML')
def _validate_xmlfmt(fd):
    source = StringIO(fd.read())
    formatted = StringIO()
    _fix_xmlfmt(source, formatted)
    return source.getvalue() == formatted.getvalue()


@message('is not valid XML')
def _validate_xml(fd):
    tree = ElementTree()
    try:
        tree.parse(fd)
    except Exception as e:
        _detail('%s: %s' % (e.__class__.__name__, e))
        return False
    return True


def _fix_xmlfmt(src, dst):
    from lxml import etree
    parser = etree.XMLParser(resolve_entities=False)
    tree = etree.parse(src, parser)
    indent_xml(tree.getroot())
    tree.write(dst, encoding='utf-8', xml_declaration=True)
    dst.write('\n')


@message('is not valid JSON')
def _validate_json(fd):
    '''
    >>> _validate_json(BytesIO(b''))
    False

    >>> _validate_json(BytesIO(b'""'))
    True
    '''
    try:
        json.loads(fd.read().decode('utf-8'))
    except Exception as e:
        _detail('%s: %s' % (e.__class__.__name__, e))
        return False
    return True


@message('is not valid YAML')
def _validate_yaml(fd):
    '''
    >>> _validate_yaml(BytesIO(b'a: b'))
    True

    >>> _validate_yaml(BytesIO(b'a: [b'))
    False
    '''
    import yaml
    try:
        # Using safeloader because it supports recursive nodes
        loader = yaml.SafeLoader(fd)
        # Support random tags
        loader.add_multi_constructor('!', (lambda _, tag, _2: tag))
        while loader.check_data():
            loader.get_data()
    except Exception as e:
        _detail('%s: %s' % (e.__class__.__name__, e))
        return False
    return True


@message('is not PythonTidy formatted')
def _validate_pythontidy(fd):
    if is_python3(fd) or running_on_py3:
        # PythonTidy supports Python 2 only
        return True
    source = StringIO(fd.read())
    if len(source.getvalue()) < 4:
        # small or empty files are ignored
        return True
    formatted = StringIO()
    PythonTidy.tidy_up(source, formatted)
    return source.getvalue() == formatted.getvalue()


@message('is not pep8 formatted')
def _validate_pep8(fd, options={}):
    import pep8

    # if user doesn't define a new value use the pep8 default
    max_line_length = options.get('max_line_length', pep8.MAX_LINE_LENGTH)

    pep8style = pep8.StyleGuide(max_line_length=max_line_length)
    check = pep8style.input_file(fd.name)
    return check == 0


def __jalopy(original, options, use_nailgun=True):
    # a temporary destination dir is needed with nailgun to prevent multiple jalopy instances from interfering
    # with each other, a temporary directory
    dest_dir = tempfile.mkdtemp('cvjalopy')
    jalopy_config = options.get('config')
    java_bin = options.get('java_bin', '/usr/bin/java')
    ng_bin = options.get('ng_bin', '/usr/bin/ng-nailgun')
    classpath = options.get('classpath')

    if use_nailgun and os.path.isfile(ng_bin):
        java_bin = ng_bin
        # loglevel has to be WARN or otherwise we get exceptions when running multiple instances
        jalopy = [java_bin, 'Jalopy', '--loglevel', 'WARN']
    elif os.path.isfile(java_bin):
        jalopy = [java_bin, '-classpath', classpath, 'Jalopy']
        if not classpath:
            raise ConfigurationError('Jalopy classpath not set')
    else:
        raise ConfigurationError('Jalopy java_bin option is invalid, %s does not exist' % java_bin)

    _env = {}
    _env.update(os.environ)
    _env['LANG'] = 'en_US.utf8'
    _env['LC_ALL'] = 'en_US.utf8'
    try:
        with NamedTemporaryFile(suffix='.java', delete=False) as f:
            f.write(original)
            f.flush()
            destination = ['--flatdest', dest_dir]
            config = (['--convention', jalopy_config] if jalopy_config else [])
            cmd = jalopy + destination + config + [f.name]
            j = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=_env)
            stdout, stderr = j.communicate()
            if stderr or '[ERROR]' in stdout:
                if stderr.strip().decode() == 'connect: Connection refused':
                    # Fallback
                    return __jalopy(original, options, use_nailgun=False)
                raise ExecutionError('Failed to execute Jalopy: %s%s' % (stderr.decode(), stdout.decode()))
            if '[WARN]' in stdout:
                logging.info('Jalopy reports warnings: %s', stdout)
            name = os.path.basename(f.name)
            result = open(os.path.join(dest_dir, name)).read()
    except:
        result = ''
    finally:
        shutil.rmtree(dest_dir, True)
    return result


@message('is not Jalopy formatted')
def _validate_jalopy(fd, options={}):
    original = fd.read()
    result = __jalopy(original, options)
    return original == result


def _fix_jalopy(src, dst, options={}):
    original = src.read()
    result = __jalopy(original, options)
    dst.write(result)


def _fix_pythontidy(src, dst):
    PythonTidy.tidy_up(src, dst)


def _fix_pep8(src, dst, options={}):
    import autopep8
    if type(src) is file:
        source = src.read()
    else:
        source = src.getvalue()

    class OptionsClass(object):

        '''Helper class for autopep8 options, just return None for unknown/new options'''

        select = options.get('select')
        ignore = options.get('ignore')
        pep8_passes = options.get('passes')
        max_line_length = options.get('max_line_length')
        verbose = False
        aggressive = True

        def __getattr__(self, name):
            return self.__dict__.get(name)

    fixed = autopep8.fix_code(source, options=OptionsClass())
    dst.write(fixed)


@message('is not phpcs (%(standard)s standard) formatted')
def _validate_phpcs(fd, options):
    """validate a PHP file to conform to PHP_CodeSniffer standards

    Needs a locally installed phpcs ("pear install PHP_CodeSniffer").
    Look at https://github.com/klaussilveira/phpcs-psr to get the PSR standard (sniffs)."""

    po = subprocess.Popen('phpcs -n --report=csv --standard=%s --encoding=%s -' % (options['standard'],
                          options['encoding']), shell=True, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                          stderr=subprocess.PIPE)
    output, stderr = po.communicate(input=fd.read())
    reader = csv.DictReader(output.split('\n'), delimiter=',', doublequote=False, escapechar='\\')
    valid = True
    for row in reader:
        valid = False
        _detail(row['Message'], line=row['Line'], column=row['Column'])
    return valid


@message('has jshint warnings/errors')
def _validate_jshint(fd, options=None):
    cfgfile = os.path.join(BASE_DIR, 'config/jshint.json')
    po = subprocess.Popen([
        'jshint',
        '--reporter=jslint',
        '--config',
        cfgfile,
        '-',
    ], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    output, stderr = po.communicate(input=fd.read())
    tree = xmlfromstring(output)
    has_errors = False
    for elem in tree.findall('.//issue'):
        _detail(elem.attrib['reason'], line=elem.attrib['line'], column=elem.attrib['char'])
        has_errors = True
    return not has_errors


@message('fails coffeelint validation')
def _validate_coffeelint(fd, options=None):
    """validate a CoffeeScript file

    Needs a locally installed coffeelint ("npm install -g coffeelint").
    """

    cfgfile = os.path.join(BASE_DIR, 'config/coffeelint.json')
    po = subprocess.Popen('coffeelint --reporter csv -s -f %s' % cfgfile, shell=True, stdin=subprocess.PIPE,
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    output, stderr = po.communicate(input=fd.read())
    valid = True
    if stderr:
        valid = False
        _detail(stderr)
    for row in output.split('\n'):
        if row and row != 'path,lineNumber,lineNumberEnd,level,message':
            valid = False
            cols = row.split(',')
            if len(cols) > 3:
                _detail(cols[3], line=cols[1])
    return valid


@message('fails puppet parser validation')
def _validate_puppet(fd):
    _env = {}
    _env.update(os.environ)
    _env['HOME'] = '/tmp'
    _env['PATH'] = '/bin:/sbin:/usr/bin:/usr/sbin'
    with tempfile.NamedTemporaryFile() as f:
        f.write(fd.read())
        f.flush()
        cmd = 'puppet parser validate --color=false --confdir=/tmp --vardir=/tmp %s' % (f.name, )
        po = subprocess.Popen(cmd.split(), stdout=subprocess.PIPE, stderr=subprocess.STDOUT, env=_env)
        output, stderr = po.communicate()
        retcode = po.poll()
        valid = True
        if output or retcode != 0:
            valid = False
            _detail('puppet parser exited with %d: %s' % (retcode, re.sub('[^A-Za-z0-9 .:-]', '', output)))
        return valid


@message('is not valid ruby')
def _validate_ruby(fd):
    p0 = subprocess.Popen(["ruby", "-c"], stdin=fd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    output, stderr = p0.communicate()
    retcode = p0.poll()
    if output.strip() != 'Syntax OK' or retcode != 0:
        _detail("ruby parser exited with %d: %s" % (retcode, stderr))
        return False
    return True


@message('is not rubocop formatted ruby code')
def _validate_rubocop(fd):
    p0 = subprocess.Popen(["rubocop", "--format", "emacs", fd.name], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    output, stderr = p0.communicate()
    retcode = p0.poll()
    if retcode != 0:
        _detail("rubocop exited with %d: \n%s" % (retcode, output))
        return False
    return True


@message('is not valid ERB template')
def _validate_erb(fd):
    p1 = subprocess.Popen([
        'erb',
        '-P',
        '-x',
        '-T',
        '-',
    ], stdin=fd, stdout=subprocess.PIPE)
    p2 = subprocess.Popen(['ruby', '-c'], stdin=p1.stdout, stdout=subprocess.PIPE)
    p1.stdout.close()
    output, stderr = p2.communicate()
    retcode = p2.poll()
    if output.strip() != 'Syntax OK' or retcode != 0:
        return False
    return True


@message('has incomplete Maven POM description')
def _validate_pomdesc(fd):
    """check Maven POM for title, description and organization"""

    NS = '{http://maven.apache.org/POM/4.0.0}'
    PROJECT_NAME_REGEX = re.compile(r'^[a-z][a-z0-9-]*$')
    tree = ElementTree()
    try:
        elem = tree.parse(fd)
    except Exception as e:
        _detail('%s: %s' % (e.__class__.__name__, e))
        return False
    # group = elem.findtext(NS + 'groupId')
    name = elem.findtext(NS + 'artifactId')
    # ver = elem.findtext(NS + 'version')
    title = elem.findtext(NS + 'name')
    if title == '${project.artifactId}':
        title = name
    description = elem.findtext(NS + 'description')
    organization = elem.findtext(NS + 'organization/' + NS + 'name')

    if not name or not PROJECT_NAME_REGEX.match(name):
        _detail('has invalid name (does not match %s)' % PROJECT_NAME_REGEX.pattern)
    if not title:
        _detail('is missing title (<name>...</name>)')
    elif title.lower() == name.lower():
        _detail('has same title as name/artifactId')
    if not description:
        _detail('is missing description (<description>..</description>)')
    elif len(description.split()) < 3:
        _detail('has a too short description')
    if not organization:
        _detail('is missing organization (<organization><name>..</name></organization>)')
    return not VALIDATION_DETAILS


@message('SQL file ends without a semicolon')
def _validate_sql_semi_colon(fd, options={}):
    import sqlparse
    sql = fd.read()
    sql_without_comments = sqlparse.format(sql, strip_comments=True).strip()
    return (sql_without_comments[-1] == ';' if sql_without_comments else True)


def _fix_sql_semi_colon(src, dst, options={}):
    original = src.read()
    dst.write(original)
    dst.write('''
;
''')


@message('doesn\'t pass Pyflakes validation')
def _validate_pyflakes(fd, options={}):
    proc = subprocess.Popen(['pyflakes', fd.name], stderr=subprocess.PIPE)
    proc.wait()
    errors = proc.stderr.read().decode().splitlines()
    for message in errors:
        error = message.message % message.message_args
        _detail(error, line=message.lineno)
    return proc.returncode == 0


@message('contains syntax errors')
def _validate_database_dir(fname, options={}):
    if 'database/lounge' in fname or not fnmatch.fnmatch(fname, '*.sql'):
        return True
    pgsqlparser_bin = options.get('pgsql-parser-bin', '/opt/codevalidator/PgSqlParser')
    if not os.path.isfile(pgsqlparser_bin):
        raise ExecutionError('PostgreSQL parser binary not found, please set "pgsql-parser-bin" option')

    try:
        with open(os.devnull, 'w') as devnull:
            return_code = subprocess.call([
                pgsqlparser_bin,
                '-q',
                '-c',
                '-i',
                fname,
            ], stderr=devnull)
        return return_code == 0
    except:
        return False


def _validate_sql_diff_dir(fname, options=None):
    allowed_file_types = [
        '*.sql_diff',
        '*.py',
        '*.yml',
        '*.txt',
        '*.md',
    ]
    if not any(fnmatch.fnmatch(fname, each) for each in allowed_file_types):
        return 'dbdiffs and migration scripts should use .sql_diff, .py, .yml, .md or .txt extension'

    dirs = get_dirs(fname)
    basedir = dirs[-2]
    filename = dirs[-1]

    if not re.match('^[A-Z]+-[0-9]+', basedir):
        return 'Patch should be located in directory with the name of the jira ticket'

    if not filename.startswith(basedir):
        return 'Filename should start with the parent directory name'

    return True


def _validate_sql_diff_sql(fname, options=None):
    head, filename = os.path.split(fname)

    if filename.endswith('.py') or filename.endswith('.yml'):
        return True

    sql = open(fname).read()
    has_set_role = re.search('[Ss][Ee][Tt] +[Rr][Oo][Ll][Ee] +[Tt][Oo] +zalando(_admin)?\s*', sql)
    has_set_project_schema_owner_role = \
        re.search('''^ *select zz_utils\.set_project_schema_owner_role\('\w+'\);''',
                  sql, re.MULTILINE + re.IGNORECASE)
    if not (has_set_role or has_set_project_schema_owner_role):
        return 'set role to zalando; or SELECT zz_utils.set_project_schema_owner_role(); must be present in db diff'

    if re.search('^ *\\\\cd +', sql, re.MULTILINE):
        return "\cd : is not allowed in db diffs anymore"

    for m in re.finditer('^ *\\\\i +([^\s]+)', sql, re.MULTILINE):
        if not m.group(1).startswith('database/'):
            return 'include path (\i ) should starts with `database/` directory'

    if fnmatch.fnmatch(filename, '*rollback*'):
        if not fnmatch.fnmatch(filename, '*.rollback.sql_diff'):
            return 'rollback script should have .rollback.sql_diff extension'
        patch_name = filename.replace('.rollback.sql_diff', '')
        re_patch_name = re.escape(patch_name)
        pattern = \
            '^ *[Ss][Ee][Ll][Ee][Cc][Tt] +_v\.unregister_patch *\( *\\\'{patch_name}\\\''.format(patch_name=re_patch_name)
        if not re.search(pattern, sql, re.MULTILINE):
            return 'unregister patch not found or patch name does not match with filename'
    else:
        patch_name = filename.replace('.sql_diff', '')
        re_patch_name = re.escape(patch_name)
        pattern = \
            '^ *[Ss][Ee][Ll][Ee][Cc][Tt] +_v\.register_patch *\( *\\\'{patch_name}\\\''.format(patch_name=re_patch_name)
        if not re.search(pattern, sql, re.MULTILINE):
            return 'register patch not found or patch name does not match with filename'

    return True


VALIDATION_ERRORS = []
VALIDATION_DETAILS = []


def _error(fname, rule, func, message=None):
    '''output the collected error messages and also print details if verbosity > 0'''

    if not message:
        message = func.message
    notify('{0}: {1}'.format(fname, message % CONFIG.get('options', {}).get(rule, {})))
    if CONFIG['verbose']:
        for message, line, column in VALIDATION_DETAILS:
            if line and column:
                notify('  line {0}, col {1}: {2}'.format(line, column, message))
            elif line:
                notify('  line {0}: {1}'.format(line, message))
            else:
                notify('  {0}'.format(message))
    VALIDATION_DETAILS[:] = []
    VALIDATION_ERRORS.append((fname, rule))


def _detail(message, line=None, column=None):
    VALIDATION_DETAILS.append((message, line, column))


def validate_file_dir_rules(fname):
    fullpath = os.path.abspath(fname)
    dirs = get_dirs(fullpath)
    dirrules = sum([CONFIG['dir_rules'][rule] for rule in CONFIG['dir_rules'] if rule in dirs], [])
    for rule in dirrules:
        logging.debug('Validating %s with %s..', fname, rule)
        func = globals().get('_validate_' + rule)
        if not func:
            notify(rule, 'does not exist')
            continue
        options = CONFIG.get('options', {}).get(rule)
        try:
            if options:
                res = func(fname, options)
            else:
                res = func(fname)
        except Exception as e:

            _error(fname, rule, func, 'ERROR validating {0}: {1}'.format(rule, e))
        else:
            if not res:
                _error(fname, rule, func)
            elif type(res) == str:
                _error(fname, rule, func, res)


def open_file_for_read(fn):
    global STDIN_CONTENTS
    if CONFIG['filter_mode']:
        if STDIN_CONTENTS is None:
            STDIN_CONTENTS = StringIO(sys.stdin.read())
            STDIN_CONTENTS.name = fn

        @contextlib.contextmanager
        def stdin_wrapper():
            STDIN_CONTENTS.seek(0)
            yield STDIN_CONTENTS
        return stdin_wrapper()
    else:
        return open(fn, 'rb')


def open_file_for_write(fn):
    if CONFIG['filter_mode']:
        return sys.stdout
    else:
        return open(fn, 'wb')


def notify(*args):
    if not CONFIG['quiet']:
        print(*args)


def validate_file_with_rules(fname, rules):
    with open_file_for_read(fname) as fd:
        for rule in rules:
            logging.debug('Validating %s with %s..', fname, rule)
            fd.seek(0)
            func = globals().get('_validate_' + rule)
            if not func:
                notify(rule, 'does not exist')
                continue
            options = CONFIG.get('options', {}).get(rule)
            try:
                if options:
                    res = func(fd, options)
                else:
                    res = func(fd)
            except Exception as e:
                _error(fname, rule, func, 'ERROR validating {0}: {1}'.format(rule, e))
            else:
                if not res:
                    _error(fname, rule, func)
                elif type(res) == str:
                    _error(fname, rule, func, res)


def validate_file(fname):
    for exclude in CONFIG['exclude_dirs']:
        if '/%s/' % exclude in fname:
            return
    head, tail = os.path.split(fname)
    for exclude in CONFIG['exclude_files']:
        if fnmatch.fnmatch(tail, exclude):
            return
    validate_file_dir_rules(fname)
    for pattern, rules in CONFIG['rules'].items():
        if fnmatch.fnmatch(fname, pattern):
            validate_file_with_rules(fname, rules)


def validate_directory(path, exclude_patterns, include_patterns):
    exclude_patterns = [os.path.join(path, pattern) for pattern in exclude_patterns or []]
    include_patterns = [os.path.join(path, pattern) for pattern in include_patterns or []]
    for root, dirnames, filenames in os.walk(path):
        for exclude in CONFIG['exclude_dirs']:
            if exclude in dirnames:
                dirnames.remove(exclude)
        for fname in filenames:
            fname = os.path.join(root, fname)
            match_excluded = any(fnmatch.fnmatch(fname, pattern) for pattern in exclude_patterns)
            match_included = any(fnmatch.fnmatch(fname, pattern) for pattern in include_patterns)

            if exclude_patterns:
                validate = not match_excluded or match_included
            else:
                validate = match_included or not include_patterns

            if validate:
                validate_file(fname)


def fix_file(fname, rules):
    was_fixed = True
    if CONFIG.get('create_backup', True):
        dirname, basename = os.path.split(fname)
        shutil.copy2(fname, os.path.join(dirname, CONFIG['backup_filename'].format(original=basename)))  # creates a backup
    with open_file_for_read(fname) as fd:
        dst = fd
        for rule in rules:
            func = globals().get('_fix_' + rule)
            if func:
                notify('{0}: Trying to fix {1}..'.format(fname, rule))
                options = CONFIG.get('options', {}).get(rule)
                src = dst
                dst = StringIO()
                src.seek(0)
                try:
                    if options:
                        func(src, dst, options)
                    else:
                        func(src, dst)
                    was_fixed &= True
                except Exception as e:
                    was_fixed = False
                    notify('{0}: ERROR fixing {1}: {2}'.format(fname, rule, e))

    fixed = (dst.getvalue() if hasattr(dst, 'getvalue') else '')
    # if the length of the fixed code is 0 we don't write the fixed version because either:
    # a) is not worth it
    # b) some fix functions destroyed the code
    if was_fixed and len(fixed) > 0:
        with open_file_for_write(fname) as fd:
            fd.write(fixed.encode())
        return True
    else:
        notify('{0}: ERROR fixing file. File remained unchanged'.format(fname))
        return False


def fix_files():
    rules_by_file = defaultdict(list)
    for fname, rule in VALIDATION_ERRORS:
        rules_by_file[fname].append(rule)
    for fname, rules in rules_by_file.items():
        fix_file(fname, rules)


def get_dirs(path):
    head, tail = os.path.split(path)
    if tail:
        return get_dirs(head) + [tail]
    else:
        return []


def main():
    parser = argparse.ArgumentParser(description='Validate source code files and optionally reformat them.')
    parser.add_argument('-r', '--recursive', action='store_true', help='process given directories recursively')
    parser.add_argument('-c', '--config',
                        help='use custom configuration file (default: ~/.codevalidatorrc or /etc/codevalidatorrc)')
    parser.add_argument('-f', '--fix', action='store_true', help='try to fix validation errors (by reformatting files)')
    parser.add_argument('-a', '--apply', metavar='RULE', action='append', help='apply the given rule(s)')
    parser.add_argument('-v', '--verbose', action='count', help='print more detailed error information (-vv for debug)')
    parser.add_argument('--no-backup', action='store_true', help='for --fix: do not create a backup file')
    parser.add_argument('--filter', action='store_true',
                        help='special mode to read from STDIN and write to STDOUT, uses provided file name to find matching rules'
                        )
    parser.add_argument('-e', '--exclude',  nargs='+', help='file patterns to exclude (only works with -r)')
    parser.add_argument('-i', '--include',  nargs='+', help='file patterns to include (only works with -r)')
    parser.add_argument('files', metavar='FILES', nargs='+', help='list of source files to validate')
    args = parser.parse_args()

    for path in DEFAULT_CONFIG_PATHS:
        config_file = os.path.expanduser(path)
        if os.path.isfile(config_file) and not args.config:
            args.config = config_file
    if args.config:
        config = open(args.config, 'rb').read().decode()
        CONFIG.update(json.loads(config))
    if args.verbose:
        CONFIG['verbose'] = args.verbose
        if args.verbose > 1:
            logging.basicConfig(level=logging.DEBUG, format='%(levelname)s %(message)s')
    if args.no_backup:
        CONFIG['create_backup'] = False

    if args.filter:
        if len(args.files) > 1:
            notify('Filter only expects exactly one file name/path')
            sys.exit(2)
        CONFIG['filter_mode'] = True
        # --fix and --filter imply quiet mode as we either print messages or output fixed file (but not both at the same time)
        CONFIG['quiet'] = args.fix
        CONFIG['create_backup'] = False

        f = args.files[0]
        validate_file(f)
        if args.fix:
            if VALIDATION_ERRORS:
                if fix_file(f, [rule for (_fn, rule) in VALIDATION_ERRORS]):
                    sys.exit(0)
                else:
                    sys.exit(1)
            else:
                # just copy STDIN to STDOUT
                with open_file_for_read(f) as stdin:
                    with open_file_for_write(f) as stdout:
                        stdout.write(stdin.read())
    else:

        for f in args.files:
            if args.recursive and os.path.isdir(f):
                validate_directory(f, args.exclude, args.include)
            elif args.apply:
                fix_file(f, args.apply)
            else:
                validate_file(f)
        if VALIDATION_ERRORS:
            if args.fix:
                fix_files()
            sys.exit(1)


if __name__ == '__main__':
    main()
