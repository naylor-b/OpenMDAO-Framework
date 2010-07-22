"""
Individual Design Feasible (IDF) Architecture
"""

from disciplines import Discipline1, Discipline2, Coupler

# pylint: disable-msg=E0611,F0401
from openmdao.lib.api import CONMINdriver
from openmdao.main.api import Assembly, set_as_top


class SellarIDF(Assembly):
    """Solution of the sellar analytical problem using IDF."""

    def __init__(self):
        """ Creates a new Assembly with this problem
        
        Optimal Design at (1.9776, 0, 0)
        
        Optimal Objective = 3.18339"""
        
        # pylint: disable-msg=E1101
        
        super(SellarIDF, self).__init__()

        # create Optimizer instance
        self.add('driver', CONMINdriver())

        # Disciplines
        self.add('coupler', Coupler())
        self.add('dis1', Discipline1())
        self.add('dis2', Discipline2())
        
        # Driver process definition
        self.driver.workflow.add([self.coupler,self.dis1,self.dis2])
        
        # Make all connections
        self.connect('coupler.z1','dis1.z1')
        self.connect('coupler.z1','dis2.z1')
        self.connect('coupler.z2','dis1.z2')
        self.connect('coupler.z2','dis2.z2')

        # Optimization parameters
        self.driver.objective = '(dis1.x1)**2 + coupler.z2 + dis1.y1 + math.exp(-dis2.y2)'
        self.driver.design_vars = ['coupler.z1_in',
                                   'coupler.z2_in',
                                   'dis1.x1',
                                   'dis2.y1',
                                   'dis1.y2']
        self.driver.constraints = ['dis2.y1-dis1.y1',
                                   'dis1.y1-dis2.y1',
                                   'dis2.y2-dis1.y2',
                                   'dis1.y2-dis2.y2']
        #self.driver.cons_is_linear = [1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1]
        self.driver.lower_bounds = [-10.0, 0.0, 0.0, 3.16, -10.0]
        self.driver.upper_bounds = [10.0, 10.0, 10.0, 10, 24.0]
        self.driver.iprint = 1
        self.driver.itmax = 100
        self.driver.fdch = .003
        self.driver.fdchm = .003
        self.driver.delfun = .0001
        self.driver.dabfun = .00001
        self.driver.ct = -.001
        self.driver.ctlmin = 0.001

        
if __name__ == "__main__": # pragma: no cover         

    import time
    
    prob = SellarIDF()
    set_as_top(prob)
    
    prob.coupler.z1_in = 5.0
    prob.coupler.z2_in = 2.0
    prob.dis1.x1 = 1.0
    prob.dis2.y1 = 3.16
    
    tt = time.time()
    prob.run()

    print "\n"
    print "CONMIN Iterations: ", prob.driver.iter_count
    print "Minimum found at (%f, %f, %f)" % (prob.coupler.z1_in, \
                                             prob.coupler.z2_in, \
                                             prob.dis1.x1)
    print "Couping vars: %f, %f" % (prob.dis1.y1, prob.dis2.y2)
    print "Minimum objective: ", prob.driver.objective.evaluate()
    print "Elapsed time: ", time.time()-tt, "seconds"

    
# End sellar-IDF.py
