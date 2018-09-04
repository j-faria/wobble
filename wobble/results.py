import numpy as np
from scipy.optimize import minimize
import h5py
import tensorflow as tf
T = tf.float64

from .utils import get_session

COMPONENT_NP_ATTRS = ['K', 'r', 'rvs_fixed', 'scale_by_airmass', 'learning_rate_rvs', 'learning_rate_template', 
                      'learning_rate_basis', 'L1_template', 'L2_template', 'L1_basis_vectors', 
                      'L2_basis_vectors', 'L2_basis_weights']
COMPONENT_TF_ATTRS = ['rvs', 'ivars', 'template_xs', 'template_ys', 'basis_vectors', 'basis_weights']
COMMON_ATTRS = ['R', 'N', 'orders', 'origin_file', 'epochs', 'component_names']

class Results(object):
    """A read/writeable object which stores RV & template results across all orders. 
    At the end of each Model optimize() call, the associated Results object is 
    updated with numpy outputs from each optimized TensorFlow variable. 
    This allows us to clear out the graph and retain the solution.
    
    One of the two keywords is required for initialization.
    
    Parameters
    ----------
    data : `object`
        a wobble Data object
    filename : str
        a file path pointing to a saved Results object (HDF5 format).
    """
    def __init__(self, data=None, filename=None):
        if filename is None:
            self.component_names = []
            self.R = data.R
            self.N = data.N
            # get everything we'd need to reconstruct the data used:
            self.orders = data.orders
            self.origin_file = data.origin_file
            self.epochs = data.epochs
            return
        if data is None:
            self.read(filename)
            return
        print("Results: must supply either data or filename keywords.")
        
    def __str__(self):
        string = 'wobble Results object consisting of the following components: '
        for n in self.component_names:
            string += '\n{0}: '.format(n)
        return string    
                            
    def add_component(self, c):
        """Initialize a new model component and prepare to save its optimized outputs. 
        The component name should be consistent across all order models. 
        
        Note that if a component name was initialized in the models for 1+ orders but 
        was not included in all order models, its RV values/uncertainties will be set 
        to NaNs and all other properties set to 0 for the excluded order(s).
        
        Parameters
        ----------
        c : a wobble.Model.Component object
        """
        if np.isin(c.name, self.component_names):
            print("Results: A component of name {0} has already been added.".format(c.name))
            return
        self.component_names.append(c.name)
        basename = c.name+'_'
        setattr(self, basename+'rvs', np.empty((self.R,self.N)) + np.nan)
        setattr(self, basename+'ivars', np.empty((self.R,self.N)) + np.nan)
        setattr(self, basename+'template_xs', [0 for r in range(self.R)])
        setattr(self, basename+'template_ys', [0 for r in range(self.R)])
        setattr(self, basename+'basis_vectors', [0 for r in range(self.R)])
        setattr(self, basename+'basis_weights', [0 for r in range(self.R)])
        setattr(self, basename+'ys_predicted', [0 for r in range(self.R)])
        for attr in COMPONENT_NP_ATTRS:
            setattr(self, basename+attr, [0 for r in range(self.R)])
                
    def update(self, c, **kwargs):
        """Update the attributes of a component from the current values of Model.
        
        Parameters
        ----------
        c : a wobble.Model.Component object
        """
        basename = c.name+'_'
        for attr in COMPONENT_NP_ATTRS:
            getattr(self, basename+attr)[c.r] = np.copy(getattr(c,attr))
        session = get_session()
        getattr(self, basename+'ys_predicted')[c.r] = session.run(c.synth, **kwargs)
        for attr in COMPONENT_TF_ATTRS:
            try:
                getattr(self, basename+attr)[c.r] = session.run(getattr(c,attr), **kwargs)
            except: # catch when basis vectors/weights don't exist
                assert c.K == 0, "Results: update() failed on attribute {0}".format(attr)
                
    def read(self, filename):
        """Write to HDF5 file."""
        print("Results: reading from {0}".format(filename))
        with h5py.File(filename,'r') as f:
            for attr in COMMON_ATTRS:
                setattr(self, attr, np.copy(f[attr]))
            self.component_names = np.copy(f['component_names'])
            self.component_names = [a.decode('utf8') for a in self.component_names] # h5py workaround
            all_order_attrs = []
            for name in self.component_names:
                basename = name + '_'
                setattr(self, basename+'ys_predicted', [0 for r in range(self.R)])
                all_order_attrs.append(basename+'ys_predicted')
                for attr in np.append(COMPONENT_NP_ATTRS, COMPONENT_TF_ATTRS):
                    setattr(self, basename+attr, [0 for r in range(self.R)])
                    all_order_attrs.append(basename+attr)
            for r in range(self.R):
                for attr in all_order_attrs:
                    getattr(self, attr)[r] = np.copy(f['order{0}'.format(r)][attr])
                
                    
    def write(self, filename):
        """Read from HDF5 file."""
        print("Results: writing to {0}".format(filename))
        with h5py.File(filename,'w') as f:            
            for r in range(self.R):
                g = f.create_group('order{0}'.format(r))
                for n in self.component_names:
                    g.create_dataset(n+'_ys_predicted', data=getattr(self, n+'_ys_predicted')[r])
                    for attr in np.append(COMPONENT_NP_ATTRS, COMPONENT_TF_ATTRS):
                        g.create_dataset(n+'_'+attr, data=getattr(self, n+'_'+attr)[r])
            self.component_names = [a.encode('utf8') for a in self.component_names] # h5py workaround
            for attr in COMMON_ATTRS:
                f.create_dataset(attr, data=getattr(self, attr))                    
                
    def combine_orders(self, component_name):
        """Calculate and save final time-series RVs for a given component after all 
        orders have been optimized.
        
        Parameters
        ----------
        component_name : str
        Name of the model component to use.
        """
        if not np.isin(component_name, self.component_names):
            print("Results: component name {0} not recognized. Valid options are: {1}".format(component_name, 
                    self.component_names))
        basename = component_name+'_'
        self.all_rvs = np.asarray(getattr(self, basename+'rvs'))
        self.all_ivars = np.asarray(getattr(self, basename+'ivars'))
        # initial guess
        x0_order_rvs = np.median(self.all_rvs, axis=1)
        x0_time_rvs = np.median(self.all_rvs - np.tile(x0_order_rvs[:,None], (1, self.N)), axis=0)
        rv_predictions = np.tile(x0_order_rvs[:,None], (1,self.N)) + np.tile(x0_time_rvs, (self.R,1))
        x0_sigmas = np.log(np.var(self.all_rvs - rv_predictions, axis=1))
        self.M = None
        # optimize
        soln_sigmas = minimize(self.opposite_lnlike_sigmas, x0_sigmas, method='BFGS', options={'disp':True})['x'] # HACK
        # save results
        lnlike, rvs_N, rvs_R = self.lnlike_sigmas(soln_sigmas, return_rvs=True)
        setattr(self, basename+'time_rvs', rvs_N)
        setattr(self, basename+'order_rvs', rvs_R)
        setattr(self, basename+'order_sigmas', soln_sigmas)
        for tmp_attr in ['M', 'all_rvs', 'all_ivars']:
            delattr(self, tmp_attr) # cleanup
        
    def lnlike_sigmas(self, sigmas, return_rvs = False, restart = False):
        """Internal code used by combine_orders()"""
        assert len(sigmas) == self.R
        M = self.get_design_matrix(restart = restart)
        something = np.zeros_like(M[0,:])
        something[self.N:] = 1. / self.R # last datum will be mean of order velocities is zero
        M = np.append(M, something[None, :], axis=0) # last datum
        Rs, Ns = self.get_index_lists()
        ivars = 1. / ((1. / self.all_ivars) + sigmas[Rs]**2) # not zero-safe
        ivars = ivars.flatten()
        ivars = np.append(ivars, 1.) # last datum: MAGIC
        MTM = np.dot(M.T, ivars[:, None] * M)
        ys = self.all_rvs.flatten()
        ys = np.append(ys, 0.) # last datum
        MTy = np.dot(M.T, ivars * ys)
        xs = np.linalg.solve(MTM, MTy)
        resids = ys - np.dot(M, xs)
        lnlike = -0.5 * np.sum(resids * ivars * resids - np.log(2. * np.pi * ivars))
        if return_rvs:
            return lnlike, xs[:self.N], xs[self.N:] # must be synchronized with get_design_matrix(), and last datum removal
        return lnlike
        
    def opposite_lnlike_sigmas(self, pars, **kwargs):
        """...the opposite of lnlike_sigmas()"""
        return -1. * self.lnlike_sigmas(pars, **kwargs)    

    def get_index_lists(self):
        """Internal code used by combine_orders()"""
        return np.mgrid[:self.R, :self.N]

    def get_design_matrix(self, restart = False):
        """Internal code used by combine_orders()"""
        if (self.M is None) or restart:
            Rs, Ns = self.get_index_lists()
            ndata = self.R * self.N
            self.M = np.zeros((ndata, self.N + self.R)) # note design choices
            self.M[range(ndata), Ns.flatten()] = 1.
            self.M[range(ndata), self.N + Rs.flatten()] = 1.
            return self.M
        else:
            return self.M