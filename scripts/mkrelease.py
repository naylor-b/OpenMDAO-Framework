
#
# An automated release script for OpenMDAO.
# For each openmdao subpackage, it creates a releaseinfo.py file and 
# builds a source distribution.
#
import sys, os
import shutil
import logging
from pkg_resources import working_set, Requirement, Environment, Distribution
from subprocess import Popen, STDOUT, PIPE
from datetime import date
from optparse import OptionParser
import tempfile


# this should contain all of the openmdao subpackages
openmdao_packages = { 'openmdao.main': ('',''), 
                      'openmdao.lib': ('','components'), 
                      'openmdao.util': ('',''), 
                      'openmdao.units': ('',''), 
                      'openmdao.recipes': ('',''),
                      'openmdao.test': ('',''), 
                      'openmdao.examples.simple': ('examples',''),
                      'openmdao.examples.bar3simulation': ('examples',''),
                      'openmdao.examples.enginedesign': ('examples',''),
                      }


relfile_template = """
# This file is automatically generated

__version__ = '%(version)s'
__revision__ = \"\"\"%(revision)s\"\"\"
__date__ = '%(date)s'
"""

def get_revision():
    try:
        p = Popen('bzr log -r-1', 
                  stdout=PIPE, stderr=STDOUT, env=os.environ, shell=True)
        out = p.communicate()[0]
        ret = p.returncode
    except:
        return 'No revision info available'
    else:
        lines = [x for x in out.split('\n') if not x.startswith('----------')
                                               and not x.startswith('Use --include-merges')]
        return '\n'.join(lines)
    
def create_releaseinfo_file(projname, version):
    """Creates a releaseinfo.py file in the current directory"""
    opts = {}
    dirs = projname.split('.')
    os.chdir(os.path.join(*dirs))
    print 'creating releaseinfo.py for %s' % projname
    f = open('releaseinfo.py', 'w')
    try:
        opts['version'] = version
        opts['date'] = date.today().isoformat()
        opts['revision'] = get_revision()
        
        f.write(relfile_template % opts)
    finally:
        f.close()

def _build_dist(build_type, destdir):
    cmd = '%s setup.py %s -d %s' % (sys.executable, build_type, destdir)
    p = Popen(cmd, stdout=PIPE, stderr=STDOUT, env=os.environ, shell=True)
    out = p.communicate()[0]
    ret = p.returncode
    if ret != 0:
        logging.error(out)
        raise RuntimeError(
             'error while building egg in %s (return code=%d): %s'
              % (os.getcwd(), ret, out))

def _build_sdist(projdir, destdir):
    """Build an sdist out of a develop egg."""
    os.chdir(projdir)
    # clean up any old builds
    if os.path.exists('build'):
        shutil.rmtree('build')
    _build_dist('sdist', destdir)
    if os.path.exists('build'):
        shutil.rmtree('build')

def _find_top_dir():
    path = os.getcwd()
    while path:
        if '.bzr' in os.listdir(path):
            return path
        path = os.path.dirname(path)
    raise RuntimeError("Can't find top dir of repository starting at %s" % os.getcwd())

    
def _create_pseudo_egg(version, destination):
    """This makes the top level openmdao egg that depends on all of the
    openmdao namespace packages.
    """
    
    setup_template = """
from setuptools import setup

setup(name='openmdao',
      version='%(version)s',
      description="A framework for multidisciplinary analysis and optimization.",
      long_description="",
      classifiers=[
        "Programming Language :: Python :: 2.6",
        "Development Status :: 2 - Pre-Alpha",
        "Topic :: Scientific/Engineering",
        "Intended Audience :: Science/Research",
        "License :: OSI Approved",
        "Natural Language :: English",
        "Operating System :: OS Independent",
        ],
      keywords='multidisciplinary optimization',
      url='http://openmdao.org',
      license='NOSA',
      namespace_packages=[],
      packages = [],
      dependency_links = [ 'http://openmdao.org/dists' ],
      zip_safe=False,
      install_requires=[
          'setuptools',
          'openmdao.lib==%(version)s',
          'openmdao.main==%(version)s',
          'openmdao.util==%(version)s',
          'openmdao.test==%(version)s',
          'openmdao.recipes==%(version)s',
      ],
      )
    """
    startdir = os.getcwd()
    tdir = tempfile.mkdtemp()
    os.chdir(tdir)
    try:
        with open('setup.py','wb') as f:
            f.write(setup_template % { 'version': version })
        os.mkdir('openmdao')
        with open(os.path.join('openmdao', '__init__.py'), 'wb') as f:
            f.write("""
try:
    __import__('pkg_resources').declare_namespace(__name__)
except ImportError:
    from pkgutil import extend_path
    __path__ = extend_path(__path__, __name__)""")
    
        _build_sdist(tdir, destination)
    finally:
        os.chdir(startdir)
        shutil.rmtree(tdir)
    
def main():
    parser = OptionParser()
    parser.add_option("-d", "--destination", action="store", type="string", dest="destdir",
                      help="directory where distributions will be placed")
    parser.add_option("","--version", action="store", type="string", dest="version",
                      help="version string applied to all openmdao distributions")
    (options, args) = parser.parse_args(sys.argv[1:])
    
    if not options.version or not options.destdir:
        parser.print_help()
        sys.exit(-1)
        
    topdir = _find_top_dir()
    destdir = os.path.realpath(options.destdir)
    if not os.path.exists(destdir):
        os.makedirs(destdir)

    startdir = os.getcwd()
    try:
        for project_name in openmdao_packages:
            pdir = os.path.join(topdir, 
                                openmdao_packages[project_name][0], 
                                project_name)
            if 'src' in os.listdir(pdir):
                os.chdir(os.path.join(pdir, 'src'))
            else:
                os.chdir(pdir)
            create_releaseinfo_file(project_name, options.version)
            print 'building %s' % project_name
            _build_sdist(pdir, destdir)
        print 'building openmdao'    
        _create_pseudo_egg(options.version, destdir)
    finally:
        os.chdir(startdir)
    
if __name__ == '__main__':
    main()
