#
# Build OpenMDAO package distributions.
#
import sys
import os
import shutil
import logging
import urllib2
from subprocess import Popen, STDOUT, PIPE, check_call
from argparse import ArgumentParser
import ConfigParser
import StringIO
import tarfile
import zipfile
import re

# get the list of openmdao subpackages from mkinstaller.py
from openmdao.devtools.mkinstaller import openmdao_dev_packages
from openmdao.devtools.build_docs import build_docs
from openmdao.devtools.utils import get_git_branch, get_git_branches, \
                                    get_git_log_info, repo_top
from openmdao.devtools.push_release import push_release
from openmdao.devtools.remote_cfg import add_config_options
from openmdao.devtools.remotetst import test_release
from openmdao.util.fileutil import cleanup
from openmdao.test.testing import read_config
from openmdao.util.fileutil import get_cfg_file, onerror

relfile_template = """
# This file is automatically generated

__version__ = '%(version)s'
__comments__ = \"\"\"%(comments)s\"\"\"
__date__ = '%(date)s'
__commit__ = '%(commit)s'
"""

_digit_rgx = re.compile('([0-9]+)')


# NOTE: this will not work correctly for release candidate versions, e.g.,
# 1.3.4rc2 will appear to be newer than 1.3.4
def _keyfunct(val):
    def cvtdigits(val):
        if val.isdigit():
            return int(val)
        return val

    parts = [s for s in re.split(_digit_rgx, val) if s]
    return tuple(map(cvtdigits, parts))


def _check_version(version):
    # first, check the form of the version
    for part in version.split('.'):
        if not part.isdigit():
            raise RuntimeError("version '%s' is not of the proper form (major.minor.revision)" %
                               version)
    pdir = 'http://openmdao.org/downloads/'
    dldirmatch = re.compile('%s[^"]+' % pdir)
    resp = urllib2.urlopen(pdir)
    dldirs = []
    for line in resp.fp:
        result = dldirmatch.search(line)
        if result is not None:
            dname = result.group()[len(pdir):]
            if dname.split('.')[0].isdigit() and '/' not in dname:
                dldirs.append(dname)

    if version in dldirs:
        raise RuntimeError("version '%s' already exists in release area" %
                           version)
    dldirs.append(version)
    lst = sorted(dldirs, key=_keyfunct, reverse=True)

    if version != lst[0]:
        raise RuntimeError("release '%s' is older than the newest existing release '%s'" %
                           (version, lst[0]))


def _get_releaseinfo_str(version):
    """Creates the content of the releaseinfo.py files"""
    opts = {}
    f = StringIO.StringIO()
    opts['version'] = version
    opts['date'] = get_git_log_info("%ci")
    opts['comments'] = get_git_log_info("%b%+s%+N").strip('"')
    opts['commit'] = get_git_log_info("%H")
    f.write(relfile_template % opts)
    return f.getvalue()


def _create_releaseinfo_file(projname, relinfo_str):
    """Creates a releaseinfo.py file in the current directory"""
    dirs = projname.split('_', 2)
    os.chdir(os.path.join(*dirs))
    print 'updating releaseinfo.py for %s' % projname
    with open('releaseinfo.py', 'w') as f:
        f.write(relinfo_str)


def _rollback_releaseinfo_file(projname):
    """Creates a releaseinfo.py file in the current directory"""
    dirs = projname.split('_', 2)
    os.chdir(os.path.join(*dirs))
    print 'rolling back releaseinfo.py for %s' % projname
    os.system('git checkout -- releaseinfo.py')


def _has_checkouts():
    cmd = 'git status -s'
    p = Popen(cmd, stdout=PIPE, stderr=STDOUT, env=os.environ, shell=True)
    out = p.communicate()[0]
    ret = p.returncode
    if ret != 0:
        logging.error(out)
        raise RuntimeError(
             'error while getting status of Git repository from directory %s (return code=%d): %s'
              % (os.getcwd(), ret, out))
    for line in out.split('\n'):
        line = line.strip()
        if len(line) > 1 and not line.startswith('?'):
            return True
    return False


def _build_dist(build_type, destdir):
    cmd = '%s setup.py %s -d %s' % (sys.executable, build_type, destdir)
    p = Popen(cmd, stdout=PIPE, stderr=STDOUT, env=os.environ, shell=True)
    out = p.communicate()[0]
    ret = p.returncode
    if ret != 0:
        logging.error(out)
        raise RuntimeError(
             'error while building %s in %s (return code=%d): %s'
              % (build_type, os.getcwd(), ret, out))


def _build_sdist(projdir, destdir, version):
    """Build an sdist out of a develop egg and place it in destdir."""
    startdir = os.getcwd()
    try:
        os.chdir(projdir)
        # clean up any old builds
        cleanup('build')
        _build_dist('sdist', destdir)
        cleanup('build')
        if sys.platform.startswith('win'):
            os.chdir(destdir)
            # unzip the .zip file and tar it up so setuptools will find it on the server
            base = os.path.basename(projdir)+'-%s' % version
            zipname = base+'.zip'
            tarname = base+'.tar.gz'
            zarch = zipfile.ZipFile(zipname, 'r')
            zarch.extractall()
            zarch.close()
            archive = tarfile.open(tarname, 'w:gz')
            archive.add(base)
            archive.close()
            cleanup(zipname, base)
    finally:
        os.chdir(startdir)


def _build_bdist_eggs(projdirs, destdir, hosts, configfile):
    """Builds binary eggs on the specified hosts and places them in destdir.
    If 'localhost' is an entry in hosts, then it builds a binary egg on the
    current host as well.
    """
    startdir = os.getcwd()
    hostlist = hosts[:]
    try:
        if 'localhost' in hostlist:
            hostlist.remove('localhost')
            for pdir in projdirs:
                os.chdir(pdir)
                _build_dist('bdist_egg', destdir)
            if sys.platform == 'darwin':
                # a dirty HACK to get easy_install to download these binaries on
                # later versions of OS X. By default, (when built on an intel mac),
                # the packages will be named *-macosx-intel.egg, and to get easy_install
                # to actually download them, we need to rename them to *-macosx-fat.egg.
                # The binaries we build contain both i386 and x86_64 architectures in
                # them, but they don't contain any PPC stuff.
                for fname in os.listdir(destdir):
                    fname = os.path.join(destdir, fname)
                    if fname.endswith('-intel.egg'):
                        newname = fname.replace('-intel.', '-fat.')
                        os.rename(fname, os.path.join(destdir, newname))

        os.chdir(startdir)
        if hostlist:
            cmd = ['remote_build',
                   '-d', destdir, '-c', configfile]
            for pdir in projdirs:
                cmd.extend(['-s', pdir])
            for host in hostlist:
                cmd.append('--host=%s' % host)
                print 'calling: %s' % ' '.join(cmd)
                check_call(cmd)
    finally:
        os.chdir(startdir)


def _update_releaseinfo_files(version):
    startdir = os.getcwd()
    topdir = repo_top()

    releaseinfo_str = _get_releaseinfo_str(version)

    pkgs = openmdao_dev_packages

    try:
        for project_name, pdir, pkgtype in pkgs:
            pdir = os.path.join(topdir, pdir, project_name)
            if 'src' in os.listdir(pdir):
                os.chdir(os.path.join(pdir, 'src'))
            else:
                os.chdir(pdir)
            _create_releaseinfo_file(project_name, releaseinfo_str)
    finally:
        os.chdir(startdir)


def _rollback_releaseinfo_files():
    startdir = os.getcwd()
    topdir = repo_top()
    
    pkgs = openmdao_dev_packages

    try:
        for project_name, pdir, pkgtype in pkgs:
            pdir = os.path.join(topdir, pdir, project_name)
            if 'src' in os.listdir(pdir):
                os.chdir(os.path.join(pdir, 'src'))
            else:
                os.chdir(pdir)
            _rollback_releaseinfo_file(project_name)
    finally:
        os.chdir(startdir)


def finalize_release(parser, options):
    """Push the specified release up to the production server and tag
    the repository with the version number.
    """
    if options.version is None:
        raise RuntimeError("you must specify the version")

    reldir = 'rel_%s' % options.version
    brname = 'release_%s' % options.version
    # check validity
    if not os.path.isdir(reldir):
        raise RuntimeError("release directory %s was not found. Did you run 'release build'?" % reldir)
    for f in os.listdir(reldir):
        if os.path.isdir(os.path.join(reldir, f)) and f != 'docs':
            raise RuntimeError("release directory is not flat. You must call 'release finalize' on the directory built by 'release build'")
    if brname not in get_git_branches():
        raise RuntimeError("branch %s doesn't exist. Did you run 'release build'?" % brname)

    if _has_checkouts():
        raise RuntimeError("the current branch still has uncommitted files")

    start_branch = get_git_branch()

    try:
        print "checking out branch %s" % brname
        check_call(['git', 'checkout', brname])
        if _has_checkouts():
            raise RuntimeError("branch %s still has uncommitted files" % brname)

        # push files up to openmdao.org
        print "pushing release files up to openmdao.org"
        if options.dry_run:
            print 'skipping...'
        else:
            check_call(['release', 'push', reldir, 'openmdao@web39.webfaction.com'])

        # push release files to official repo on github (master branch)
        print "pushing branch %s up to the official master branch" % brname
        if options.dry_run:
            print 'skipping...'
        else:
            check_call(['git', 'push', '--tags', 'origin', '%s:master' % brname])

    finally:
        print 'returning to original branch (%s)' % start_branch
        check_call(['git', 'checkout', start_branch])


def _get_cfg_val(config, section, option):
    """Just returns None if option isn't there, rather than raising an exception."""
    try:
        return config.get(section, option)
    except ConfigParser.NoOptionError:
        return None


def build_release(parser, options):
    """Create an OpenMDAO release, placing the following files in the
    specified destination directory:

        - source distribs of all of the openmdao subpackages
        - binary eggs for openmdao subpackages with compiled code
        - an installer script for the released version of openmdao that will
          create a virtualenv and populate it with all of the necessary
          dependencies needed to use openmdao
        - Sphinx documentation in html

    To run this, you must be in a Git repository with no uncommitted
    changes. If not running with the ``--test`` option, a release branch will be
    created from the specified base branch, and in the process of running, a
    number of ``releaseinfo.py`` files will be updated with new version
    information and committed.
    """

    if options.version is None:
        parser.print_usage()
        print "version was not specified"
        sys.exit(-1)

    if options.destdir is None:
        options.destdir = "rel_%s" % options.version

    _check_version(options.version)

    options.cfg = os.path.expanduser(options.cfg)

    hostlist, config = read_config(options)
    required_binaries = set([('windows', 'python2.7')])
    binary_hosts = set()
    if options.binaries:
        for host in hostlist:
            if config.has_section(host):
                if config.has_option(host, 'build_binaries') and config.getboolean(host, 'build_binaries'):
                    platform = _get_cfg_val(config, host, 'platform')
                    py = _get_cfg_val(config, host, 'py')
                    if (platform, py) in required_binaries:
                        required_binaries.remove((platform, py))
                        binary_hosts.add(host)

        if sys.platform == 'darwin':  # build osx binaries if we're on a mac
            binary_hosts.add('localhost')
        elif required_binaries and sys.platform.startswith('win'):
            try:
                required_binaries.remove(('windows', 'python%d.%d' % (sys.version_info[0:2])))
            except:
                pass
            else:
                binary_hosts.add('localhost')

    if required_binaries:
        print "WARNING: binary distributions are required for the following and no hosts were specified: %s" % list(required_binaries)
        if not options.test:
            print 'aborting...'
            sys.exit(-1)

    orig_branch = get_git_branch()
    if not orig_branch:
        print "You must make a release from within a git repository. aborting"
        sys.exit(-1)

    if not options.test:
        if orig_branch != options.base:
            print "Your current branch '%s', is not the specified base branch '%s'" % (orig_branch, options.base)
            sys.exit(-1)

        if _has_checkouts():
            print "There are uncommitted changes. You must create a release from a clean branch"
            sys.exit(-1)

        if orig_branch == 'master':
            print "pulling master branch from origin..."
            os.system("git pull origin %s" % orig_branch)
            if _has_checkouts():
                print "something went wrong during pull.  aborting"
                sys.exit(-1)
        else:
            print "WARNING: base branch is not 'master' so it has not been"
            print "automatically brought up-to-date."
            answer = raw_input("Proceed? (Y/N) ")
            if answer.lower() not in ["y", "yes"]:
                sys.exit(-1)

        relbranch = "release_%s" % options.version
        if relbranch in get_git_branches():
            print "release branch %s already exists in this repo" % relbranch
            sys.exit(-1)

        print "creating release branch '%s' from base branch '%s'" % (relbranch, orig_branch)
        check_call(['git', 'branch', relbranch])
        print "checking out branch '%s'" % relbranch
        check_call(['git', 'checkout', relbranch])

    destdir = os.path.abspath(options.destdir)
    if not os.path.exists(destdir):
        os.makedirs(destdir)

    startdir = os.getcwd()
    topdir = repo_top()

    cfgpath = os.path.expanduser(options.cfg)

    try:
        _update_releaseinfo_files(options.version)

        # build the docs
        docdir = os.path.join(topdir, 'docs')
        idxpath = os.path.join(docdir, '_build', 'html', 'index.html')

        if not os.path.isfile(idxpath) or not options.nodocbuild:
            build_docs(parser, options)

        shutil.copytree(os.path.join(topdir, 'docs', '_build', 'html'),
                    os.path.join(destdir, 'docs'))

        shutil.copytree(os.path.join(topdir, 'docs', '_build', 'html'),
                    os.path.join(topdir, 'openmdao_main', 'src', 'openmdao', 'main', 'docs'))

        if not options.test:
            # commit the changes to the release branch
            print "committing all changes to branch '%s'" % relbranch
            check_call(['git', 'commit', '-a', '-m',
                        '"updating releaseinfo files for release %s"' %
                        options.version])

        # build openmdao package distributions
        proj_dirs = []
        for project_name, pdir, pkgtype in openmdao_dev_packages[:-1]:
            pdir = os.path.join(topdir, pdir, project_name)
            if 'src' in os.listdir(pdir):
                os.chdir(os.path.join(pdir, 'src'))
            else:
                os.chdir(pdir)
            print 'building %s' % project_name
            _build_sdist(pdir, destdir, options.version)
            if pkgtype == 'bdist_egg':
                proj_dirs.append(pdir)

        os.chdir(startdir)
        _build_bdist_eggs(proj_dirs, destdir, list(binary_hosts), cfgpath)

        print 'creating bootstrapping installer script go-openmdao-%s.py' % options.version
        installer = os.path.join(os.path.dirname(__file__),
                                 'mkinstaller.py')

        check_call([sys.executable, installer, '--dest=%s' % destdir])

        if options.comment:
            comment = options.comment
        else:
            comment = 'creating release %s' % options.version

        if not options.test:
            # tag the current revision with the release version id
            print "tagging release with '%s'" % options.version
            check_call(['git', 'tag', '-f', '-a', options.version, '-m', comment])

            check_call(['git', 'checkout', orig_branch])
            print "\n*REMEMBER* to push '%s' up to the master branch if this release is official" % relbranch

        print "new release files have been placed in %s" % destdir

    finally:
        if options.test:
            _rollback_releaseinfo_files()
        #Cleanup
        try:
            shutil.rmtree(os.path.join(topdir, "openmdao_main", 'src', 'openmdao', 'main', "docs"))
        except:
            pass
        os.chdir(startdir)


def _get_release_parser():
    """Sets up the 'release' arg parser and all of its subcommand parsers."""

    top_parser = ArgumentParser()
    subparsers = top_parser.add_subparsers(title='subcommands')

    parser = subparsers.add_parser('finalize',
               description="push the release to the production area and tag the production repository")
    parser.add_argument("-v", "--version", action="store", type=str,
                        dest="version",
                        help="release version of OpenMDAO to be finalized")
    parser.add_argument("-d", "--dryrun", action="store_true", dest='dry_run',
                        help="don't actually push any changes up to github or openmdao.org")
    parser.add_argument("--tutorials", action="store_true", dest="tutorials",
                        help="Only upload the tutorials, no other action will be taken")
    parser.set_defaults(func=finalize_release)

    parser = subparsers.add_parser('push',
               description="push release dists and docs into an OpenMDAO release directory structure (downloads, dists, etc.)")
    parser.usage = "%(prog)s releasedir destdir [options] "

    parser.add_argument('releasedir', nargs='?',
                        help='directory where release files are located')
    parser.add_argument('destdir', nargs='?',
                        help='location where structured release files will be placed')
    parser.add_argument("--py", action="store", type=str, dest="py",
                        default="python",
                        help="python version to use on target host")
    parser.set_defaults(func=push_release)

    parser = subparsers.add_parser('test',
                                   description="test an OpenMDAO release")
    parser.add_argument('fname', nargs='?',
                        help='pathname of release directory or go-openmdao-<version>.py file')
    add_config_options(parser)
    parser.add_argument("-k", "--keep", action="store_true", dest='keep',
                      help="Don't delete the temporary build directory. "
                           "If testing on EC2 stop the instance instead of terminating it.")
    parser.add_argument("--testargs", action="store", type=str, dest='testargs',
                        default='',
                        help="args to be passed to openmdao test")
    parser.set_defaults(func=test_release)

    parser = subparsers.add_parser('build',
                                   description="create release versions of all OpenMDAO dists")
    parser.add_argument("-d", "--dest", action="store", type=str,
                        dest="destdir",
                        help="directory where all release distributions and docs will be placed")
    parser.add_argument("-v", "--version", action="store", type=str,
                        dest="version",
                        help="version string applied to all openmdao distributions")
    parser.add_argument("-m", action="store", type=str, dest="comment",
                        help="optional comment for version tag")
    parser.add_argument("--basebranch", action="store", type=str,
                        dest="base", default='master',
                        help="base branch for release. defaults to master")
    parser.add_argument("-t", "--test", action="store_true", dest="test",
                        help="used for testing. A release branch will not be created")
    parser.add_argument("-n", "--nodocbuild", action="store_true",
                        dest="nodocbuild",
                        help="used for testing. The docs will not be rebuilt if they already exist")
    parser.add_argument("-b", "--binaries", action="store_true",
                        dest="binaries",
                        help="build binary distributions where necessary")
    parser.add_argument("--host", action='append', dest='hosts', metavar='HOST',
                        default=[],
                        help="host from config file to build bdist_eggs on. "
                           "Multiple --host args are allowed.")
    parser.add_argument("-c", "--config", action='store', dest='cfg',
                        metavar='CONFIG', default=get_cfg_file(),
                        help="path of config file where info for hosts is located")
    parser.set_defaults(func=build_release)

    return top_parser


def release():
    parser = _get_release_parser()
    options = parser.parse_args()
    options.func(parser, options)


if __name__ == '__main__':
    release()
