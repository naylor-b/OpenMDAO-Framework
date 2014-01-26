#!/bin/bash

cd openmdao.util
$PYTHON setup.py install
cd ../openmdao.units
$PYTHON setup.py install
cd ../openmdao.main
$PYTHON setup.py install
cd ../openmdao.lib
$PYTHON setup.py install
cd ../openmdao.test
$PYTHON setup.py install
cd ../openmdao.gui
$PYTHON setup.py install

cd ../examples/openmdao.examples.simple
$PYTHON setup.py install
cd ../openmdao.examples.mdao
$PYTHON setup.py install
cd ../openmdao.examples.metamodel_tutorial
$PYTHON setup.py install
cd ../openmdao.examples.nozzle_geometry_doe
$PYTHON setup.py install
cd ../openmdao.examples.expected_improvement
$PYTHON setup.py install
cd ../openmdao.examples.enginedesign
$PYTHON setup.py install
cd ../openmdao.examples.bar3simulation
$PYTHON setup.py install
cd ../..

