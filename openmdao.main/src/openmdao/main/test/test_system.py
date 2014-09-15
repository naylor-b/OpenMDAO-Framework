
import unittest

import time
from numpy import array, ones

from openmdao.main.api import Component, Driver, Assembly, set_as_top
from openmdao.main.datatypes.api import Float, Array
from openmdao.main.hasparameters import HasParameters
from openmdao.main.hasobjective import HasObjective
from openmdao.main.hasconstraints import HasConstraints
from openmdao.main.interfaces import IHasParameters, implements
from openmdao.util.decorators import add_delegate
from openmdao.util.testutil import assert_rel_error

class Paraboloid(Component):
    """ Evaluates the equation f(x,y) = (x-3)^2 + xy + (y+4)^2 - 3 """

    # set up interface to the framework
    # pylint: disable=E1101
    x = Float(0.0, iotype='in', desc='The variable x')
    y = Float(0.0, iotype='in', desc='The variable y')

    f_xy = Float(iotype='out', desc='F(x,y)')

    def execute(self):
        """f(x,y) = (x-3)^2 + xy + (y+4)^2 - 3
        Optimal solution (minimum): x = 6.6667; y = -7.3333
        """

        x = self.x
        y = self.y

        self.f_xy = (x-3.0)**2 + x*y + (y+4.0)**2 - 3.0

    def provideJ(self):
        """Analytical first derivatives"""

        df_dx = 2.0*self.x - 6.0 + self.y
        df_dy = 2.0*self.y + 8.0 + self.x

        self.J = array([[df_dx, df_dy]])
        return self.J

    def list_deriv_vars(self):
        input_keys = ('x', 'y')
        output_keys = ('f_xy',)
        return input_keys, output_keys


@add_delegate(HasParameters, HasObjective, HasConstraints)
class SimpleDriver(Driver):
    """Driver with Parameters"""

    implements(IHasParameters)


class TestcaseParaboloid(unittest.TestCase):
    def setUp(self):
        self.top = top = set_as_top(Assembly())
        top.add('comp', Paraboloid())
        top.add('driver', SimpleDriver())
        top.driver.workflow.add(['comp'])

    def test_single_comp(self):
        top = self.top

        top.driver.add_parameter('comp.x', low=-1000, high=1000)
        top.driver.add_parameter('comp.y', low=-1000, high=1000)
        top.driver.add_objective('comp.f_xy')
        top.comp.x = 3
        top.comp.y = 5

        top.run()

        # See if model gets the right answer
        self.assertEqual(top.comp.f_xy, 93.)
        self.assertEqual(top._pseudo_0.in0, 93.)
        self.assertEqual(top._pseudo_0.out0, 93.)

    def test_boundary_out(self):
        top = self.top

        top.driver.add_parameter('comp.x', low=-1000, high=1000)
        top.driver.add_parameter('comp.y', low=-1000, high=1000)
        top.driver.add_objective('comp.f_xy')

        top.create_passthrough('comp.f_xy')

        top.comp.x = 3
        top.comp.y = 5

        top.run()

        # See if model gets the right answer
        self.assertEqual(top.f_xy, 93.)

    def test_boundary_in_out(self):
        top = self.top

        top.driver.add_parameter('comp.y', low=-1000, high=1000)
        top.driver.add_objective('comp.f_xy')

        top.create_passthrough('comp.x')
        top.create_passthrough('comp.f_xy')

        top.x = 3
        top.comp.y = 5

        top.run()

        # See if model gets the right answer
        self.assertEqual(top.f_xy, 93.)


class ABCDArrayComp(Component):
    delay = Float(0.01, iotype='in')

    def __init__(self, arr_size=9):
        super(ABCDArrayComp, self).__init__()
        self.add_trait('a', Array(ones(arr_size, float), iotype='in'))
        self.add_trait('b', Array(ones(arr_size, float), iotype='in'))
        self.add_trait('c', Array(ones(arr_size, float), iotype='out'))
        self.add_trait('d', Array(ones(arr_size, float), iotype='out'))

    def execute(self):
        time.sleep(self.delay)
        self.c = self.a + self.b
        self.d = self.a - self.b


class TestArrayComp(unittest.TestCase):
    def test_overlap_exception(self):
        size = 20   # array var size

        top = set_as_top(Assembly())
        top.add("C1", ABCDArrayComp(size))
        top.add("C2", ABCDArrayComp(size))
        top.add("C3", ABCDArrayComp(size))
        top.add("C4", ABCDArrayComp(size))
        top.driver.workflow.add(['C1', 'C2', 'C3', 'C4'])
        top.connect('C1.c[:5]', 'C2.a')
        top.connect('C1.c[3:]', 'C3.b')
        top.connect('C2.c', 'C4.a')
        top.connect('C3.d', 'C4.b')

        top.C1.a = ones(size, float) * 3.0
        top.C1.b = ones(size, float) * 7.0

        try:
            top.run()
        except Exception as err:
            self.assertEqual(str(err), "Subvars ['C1.c[3::]', 'C1.c[:5:]'] share overlapping indices. Try reformulating the problem to prevent this.")
        else:
            self.fail("Exception expected")
