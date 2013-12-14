import os
import sys
import time
from datetime import datetime
import traceback
import sqlite3
from argparse import ArgumentParser

from nose.plugins import Plugin
from nose import SkipTest, main

__version__ = 0.8

class DBWrapper(object):
    def __init__(self, path, new=False):
        if new and os.path.isfile(path):
            os.remove(path)

        self._connection = sqlite3.connect(path)
        self.create_tables()

    def create_tables(self):
        """Creates tables in the test db"""

        self._connection.execute("""
        CREATE TABLE if not exists tests (
           id INTEGER PRIMARY KEY,
           elapsed_time REAL,
           run_id INTEGER,
           traceback TEXT,
           status TEXT
         )""")
        
        # the testruns table has an entry for each execution
        # of a 'test' command from the command line.  Each
        # command line run can execute from 1 to many tests
        self._connection.execute("""
        CREATE TABLE if not exists testruns(
           id INTEGER PRIMARY KEY,
           cmd TEXT,
           date TEXT,
           passes INTEGER,
           fails INTEGER,
           errors INTEGER,
           skips INTEGER,
           elapsed_time REAL
         )""")

    def query(self, sql):
        cur = self._connection.cursor()
        cur.execute(sql)
        for tup in cur:
            yield tup

    def commit(self):
        self._connection.commit()

    def set(self, table, where=(), **kwargs):
        cur = self._connection.cursor()
        sql = ['update %s set' % table]
        sql2 = []
        for name, val in kwargs.items():
            sql2.append('%s=%s' % (name,val))
        sql.append(','.join(sql2))
        if where:
            sql.append('where')
        for w in where:
            sql.append(w)
        print ' '.join(sql)
        cur.execute(' '.join(sql))
        return cur

    def insert(self, table, **kwargs):
        cur = self._connection.cursor()
        sql = 'insert into %s%s values (%s)' % (table, 
                                                tuple(kwargs.keys()), 
                                                ','.join(['?']*len(kwargs)))
        cur.execute(sql, tuple(kwargs.values()))
        return cur

    def close(self):
        """Commit and close DB connection"""
        if self._connection is not None:
            self._connection.commit()
            self._connection.close()
            self._connection = None


class TestInfo(object):
    def __init__(self, test):
        self.start = time.time()
        self.name = test.shortDescription()
        if self.name is None:
            self.name = test.id()
        self.status = None
        self.elapsed = 0.
        self.traceback = ''

    def end(self):
        self.elapsed = time.time() - self.start


class TestDB(Plugin):
    """This plugin writes test info (failures, run time) to 
    a sqlite database for later querying.  Use --with-db to 
    activate it.
    """

    name = 'db'
    score = 1001 # need high score to get called before ErroClassPlugin,
                 # otherwise we lose Skips

    def options(self, parser, env):
        """Sets additional command line options."""
        parser.add_option("--db", action="store", type="string",
                          dest="db",
                          default="testing.db",
                          help="name of database file. (defaults to 'testing.db')")
        parser.add_option("--new", action="store_true",
                          dest="new",
                          help="if set, overwrite the db file if it exists")
        super(TestDB, self).options(parser, env)

    def configure(self, options, config):
        """Configures the plugin."""
        super(TestDB, self).configure(options, config)
        self.config = config
        self._tests = {}
        self._fails = 0
        self._errors = 0
        self._skips = 0
        self._passes = 0
        self._elapsed_time = 0.
        self._options = options
        self._dbfile = options.db

    def formatErr(self, err):
        exctype, value, tb = err
        return ''.join(traceback.format_exception(exctype, value, tb))
    
    def record(self, testinfo):
        """Record the given test."""
        self.db.insert("tests",
                        elapsed_time=testinfo.elapsed,
                        run_id=self.run_id,
                        traceback=testinfo.traceback,
                        status=testinfo.status)
        self.db.commit()
    
    def _end_test(self, testinfo):
        testinfo.end()
        self.record(testinfo)

    def addError(self, test, err, capt=None):
        testinfo = self._tests[id(test)]
        if err[0] == SkipTest:
            testinfo.status = 'S'
            self._skips += 1
        else:
            testinfo.status = 'E'
            self._errors += 1
        testinfo.traceback = self.formatErr(err)
        self._end_test(testinfo)

    def addFailure(self, test, err, capt=None, tb_info=None):
        testinfo = self._tests[id(test)]
        testinfo.status = 'F'
        self._fails += 1
        testinfo.traceback = self.formatErr(err)
        self._end_test(testinfo)

    def addSuccess(self, test, capt=None):
        testinfo = self._tests[id(test)]
        testinfo.status = 'G'
        self._passes += 1
        self._end_test(testinfo)
        
    def begin(self):
        self.db = DBWrapper(self._options.db, 
                            self._options.new)
        cursor = self.db.insert('testruns', 
                                   cmd=' '.join(sys.argv),
                                   date=datetime.now())
        self.db.commit()
        self.run_id = cursor.lastrowid

    def finalize(self, result):
        self.db.set('testruns',
                    where=['id=%d' % self.run_id],
                    passes=self._passes,
                    fails=self._fails,
                    errors=self._errors,
                    skips=self._skips,
                    elapsed_time=0.)
        self.db.close()
    
    def beforeTest(self, test):
        self._tests[id(test)] = TestInfo(test)
        
    def stopTest(self, test):
        pass


def _get_testdb_parser():
    """Sets up the plugin arg parser and all of its subcommand parsers."""

    top_parser = ArgumentParser()
    top_parser.add_argument('-v', '--version', action='version',
                            version='testdb %s' % __version__)

    subparsers = top_parser.add_subparsers(title='commands')

    parser = subparsers.add_parser('summary',
                                   help='list summary info for test runs')
    parser.add_argument('-l', '--latest', action='store_true', dest='latest',
                        help='Get summary for latest test run')
    parser.add_argument('--db', action='store', dest='db', 
                        metavar='DB', default='testing.db',
                        help='specify the test database file')
    parser.add_argument('--start', action='store', dest='start', 
                        metavar='START', 
                        help='show testrun summaries after this date')
    parser.add_argument('--end', action='store', dest='end', 
                        metavar='END', 
                        help='show testrun summaries before this date')
    parser.add_argument('--all', action='store_true', dest='all',
                        help='list summary info for all test runs')
    parser.set_defaults(func=summary)

    parser = subparsers.add_parser('tests',
                                   help='list info for individual tests')
    parser.add_argument('--db', action='store', dest='db', 
                        metavar='DB', default='testing.db',
                        help='specify the test database file')
    parser.add_argument('--start', action='store', dest='start', 
                        metavar='START', 
                        help='show tests after this date')
    parser.add_argument('--end', action='store', dest='end', 
                        metavar='END', 
                        help='show tests before this date')
    parser.add_argument('--all', action='store_true', dest='all',
                        help='list info for all tests')
    parser.set_defaults(func=tests)

    parser = subparsers.add_parser('dump', 
                                    help='dump all tables in the test db')
    parser.add_argument("--db", action="store", 
                        dest='db', default='testing.db',
                        help="test database file")
    parser.set_defaults(func=dump)


    return top_parser


def _display(cursor):
    for line in cursor:
        print line

def dump(parser, options, args):
    conn = sqlite3.connect(options.db)

    cmd = "SELECT name from sqlite_master WHERE type='table' ORDER BY name;"
    cur = conn.cursor()
    cur.execute(cmd)
    
    for n in cur:
        print '\nTable %s' % n[0]
        cur2 = conn.cursor()
        cur2.execute("SELECT * from %s;" % n[0])
        for result in cur2:
            for r in result:
                if isinstance(r, basestring):
                    print r[0:50],', ',
                else:
                    print r,', ',
            print ''

def tests(parser, options, args):
    pass

def summary(parser, options, args):
    if options.all:
        query = 'SELECT * from testruns'
    elif options.start or options.end:
        pass
    elif options.latest:
        query = 'SELECT * from testruns ORDER BY id DESC LIMIT 1'

    db = DBWrapper(options.db)

    for line in db.query(query):
        print line

def testdb():
    """A command line interface for querying a test database."""
    parser = _get_testdb_parser()
    options, args = parser.parse_known_args()

    sys.exit(options.func(parser, options, args))


if __name__ == '__main__':
    #main(addplugins=[TestDB()])

    db = DBWrapper('testing.db')
    for line in db.query("SELECT * from tests"):
        print line



