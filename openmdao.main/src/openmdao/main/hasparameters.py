
from enthought.traits.api import HasTraits

import ordereddict

from openmdao.lib.traits.float import Float
from openmdao.lib.traits.int import Int
from openmdao.lib.traits.enum import Enum
from openmdao.lib.traits.array import Array
from enthought.traits.api import implements

from openmdao.main.expreval import ExprEvaluator

class _Parameter(object): 
    
    def __init__(self, low=None, high=None, expr=None):
        self.low = low
        self.high = high
        self.expreval = expr

class HasParameters(object): 
    """This class provides an implementation of the IHasParameters interface"""

    def __init__(self, parent):
        self._parameters = ordereddict.OrderedDict()
        self._parent = parent

    def add_parameter(self, name, low=None, high=None):
        """Adds a parameter to the driver. 
        
        name : string
            name of the public variable the driver should vary during execution.
            
        low : float, optional
            minimum allowed value of the parameter
            
        high : float, optional
            maximum allowed value of the parameter
        
        If neither 'low' nor 'high' are specified, the min and max will
        default to the values in the metadata of the public variable being
        referenced. If they are not specified in the metadata and not provided
        as arguments a ValueError is raised.
        """
        if name in self._parameters: 
            self._parent.raise_exception("Trying to add parameter '%s' to driver, "
                                         "but it's already there" % name,
                                         AttributeError)
        
        parameter = _Parameter()
        parameter.expreval = ExprEvaluator(name, self._parent.parent, single_name=True) 
        
        try:
            metadata = self._parent.parent.get_metadata(name)
        except AttributeError:
            self._parent.raise_exception("Can't add parameter '%s' because it doesn't exist." % name,
                                         AttributeError)
        
        meta_low = metadata.get('low') # this will be None if 'low' isn't there
        if low is None:
            parameter.low = meta_low
        else:  # low is not None
            if meta_low is not None and low < meta_low:
                self._parent.raise_exception("Trying to add parameter '%s', " 
                                             "but the lower limit supplied (%s) exceeds the " 
                                             "built-in lower limit (%s)." % 
                                             (name, low, meta_low), ValueError)
            parameter.low = low

        meta_high = metadata.get('high') # this will be None if 'high' isn't there
        if high is None:
            parameter.high = meta_high
        else:  # high is not None
            if meta_high is not None and high > meta_high:
                self._parent.raise_exception("Trying to add parameter '%s', " 
                                             "but the upper limit supplied (%s) exceeds the " 
                                             "built-in upper limit (%s)." % 
                                             (name, high, meta_high), ValueError)
            parameter.high = high
            
        if parameter.low is None: 
            self._parent.raise_exception("Trying to add parameter '%s', "
                                         "but no lower limit was found and no " 
                                         "'low' argument was given. One or the "
                                         "other must be specified." % name,ValueError)
        if parameter.high is None: 
            self._parent.raise_exception("Trying to add parameter '%s', "
                                         "but no upper limit was found and no " 
                                         "'high' argument was given. One or the "
                                         "other must be specified." % name,ValueError)
        
        self._parameters[name] = parameter
            
    def remove_parameter(self, name):
        """Removes the parameter with the given name."""
        try:
            del self._parameters[name]
        except KeyError:
            self._parent.raise_exception("Trying to remove parameter '%s' "
                                         "that is not in the driver." % name,
                                         AttributeError)
    def list_parameters(self):
        """Returns an alphabetized list of parameter names."""
        return sorted(self._parameters.keys())
    
    def clear_parameters(self):
        """Removes all parameters."""
        self._parameters = ordereddict.OrderedDict()
        
    def get_parameters(self):
        """Returns a list of parameter objects."""
        return self._parameters
                
             
            #elif '[' in target: #can't figure out what the ranges should be
                #if not(isinstance(val,float) or isinstance(val,int) or isinstance(val,int32) or \
                       #isinstance(val,int64) or isinstance(val,float32) or isinstance(val,float64)
                      #):
                    #self.raise_exception("Only array values of type 'int' or 'float' are allowed as "
                                         #"parameters")
                    
                #self.raise_exception("values for 'high' and 'low' arguments are required when specifying "
                                     #"an Array element as a parameter. They were not given for '%s'"%param_name,TypeError)
        
    def set_parameters(self, X): 
        """Pushes the values in the X input array into the corresponding public 
        variables in the model.
        
        X: iterator
            iterator of input values with an order defined to match the order of parameters returned 
            by the list_parameter method. X must support the len() function.
        """
        if len(X) != len(self._parameters):
            raise ValueError("number of input values (%s) != number of parameters (%s)" % 
                             (len(X),len(self._parameters)))

        for x, (param_name, param) in zip(X, self._parameters.iteritems()): 
            param.expreval.set(x)
