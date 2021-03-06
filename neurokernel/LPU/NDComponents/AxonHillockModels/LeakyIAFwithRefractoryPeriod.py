
from neurokernel.LPU.NDComponents.AxonHillockModels.BaseAxonHillockModel import *

class LeakyIAFwithRefractoryPeriod(BaseAxonHillockModel):
    updates = ['spike_state', # (bool)
               'V' # Membrane Potential (mV)
              ]
    accesses = ['I'] # (\mu A/cm^2 )
    params = ['resting_potential', # (mV)
              'threshold', # Firing Threshold (mV)
              'reset_potential', # Potential to be reset to after a spike (mV)
              'capacitance', # (\mu F/cm^2)
              'refractory_period', # (ms)
              'time_constant', # (ms)
              'bias_current' # (\mu A/cm^2)
              ]
    internals = OrderedDict([('internalV', 0.0), # Membrane Potential (mV)
                             ('refractory_time_left', 0.0) # (ms)
                            ])

    def pre_run(self, update_pointers):
        self.add_initializer('resting_potential', 'internalV', update_pointers)
        self.add_initializer('resting_potential', 'V', update_pointers)

    def get_update_template(self):
        template = """
__global__ void update(int num_comps, %(dt)s dt, int nsteps,
               %(I)s* g_I,
               %(resting_potential)s* g_resting_potential,
               %(threshold)s* g_threshold,
               %(reset_potential)s* g_reset_potential,
               %(capacitance)s* g_capacitance,
               %(resistance)s* g_resistance,
               %(refractory_period)s* g_refractory_period,
               %(time_constant)s* g_time_constant,
               %(bias_current)s* g_bias_current,
               %(internalV)s* g_internalV,
               %(refractory_time_left)s* g_refractory_time_left,
               %(spike_state)s* g_spike_state, %(V)s* g_V)
{
    int tid = threadIdx.x + blockIdx.x * blockDim.x;
    int total_threads = gridDim.x * blockDim.x;

    %(dt)s ddt = dt*1000.; // s to ms
    %(V)s V;
    %(I)s I;
    %(spike_state)s spike;
    %(resting_potential)s resting_potential;
    %(threshold)s threshold;
    %(reset_potential)s reset_potential;
    %(capacitance)s capacitance;
    %(time_constant)s time_constant;
    %(bias_current)s bias_current;
    %(internalV)s internalV;
    %(refractory_time_left)s refractory_time_left;
    %(dt)s bh;

    for(int i = tid; i < num_comps; i += total_threads)
    {
        spike = 0;
        refractory_time_left = fmax%(fletter)s(g_refractory_time_left[i] - ddt, 0);
        V = g_internalV[i];
        I = g_I[i];
        time_constant = g_time_constant[i];
        capacitance = g_capacitance[i];
        reset_potential = g_reset_potential[i];
        resting_potential = g_resting_potential[i];
        threshold = g_threshold[i];
        bias_current = g_bias_current[i];

        bh = exp%(fletter)s(-ddt/time_constant);

        for (int j = 0; j < nsteps; ++j)
        {
            V = V*bh + ((refractory_time_left == 0 ? time_constant/capacitance*(I+bias_current) : 0) + resting_potential) * (1.0 - bh);


            if (V >= threshold)
            {
                V = reset_potential;
                spike = 1;
                refractory_time_left += g_refractory_period[i];
            }
        }

        g_V[i] = V;
        g_internalV[i] = V;
        g_spike_state[i] = spike;
        g_refractory_time_left[i] = refractory_time_left;

    }
}
        """
        return template

if __name__ == '__main__':
    import argparse
    import itertools
    import networkx as nx
    from neurokernel.tools.logging import setup_logger
    import neurokernel.core_gpu as core

    from neurokernel.LPU.LPU import LPU

    from neurokernel.LPU.InputProcessors.FileInputProcessor import FileInputProcessor
    from neurokernel.LPU.InputProcessors.StepInputProcessor import StepInputProcessor
    from neurokernel.LPU.OutputProcessors.FileOutputProcessor import FileOutputProcessor

    import neurokernel.mpi_relaunch

    dt = 1e-4
    dur = 1.0
    steps = int(dur/dt)

    parser = argparse.ArgumentParser()
    parser.add_argument('--debug', default=False,
                        dest='debug', action='store_true',
                        help='Write connectivity structures and inter-LPU routed data in debug folder')
    parser.add_argument('-l', '--log', default='both', type=str,
                        help='Log output to screen [file, screen, both, or none; default:none]')
    parser.add_argument('-s', '--steps', default=steps, type=int,
                        help='Number of steps [default: %s]' % steps)
    parser.add_argument('-g', '--gpu_dev', default=0, type=int,
                        help='GPU device number [default: 0]')
    args = parser.parse_args()

    file_name = None
    screen = False
    if args.log.lower() in ['file', 'both']:
        file_name = 'neurokernel.log'
    if args.log.lower() in ['screen', 'both']:
        screen = True
    logger = setup_logger(file_name=file_name, screen=screen)

    man = core.Manager()

    G = nx.MultiDiGraph()

    G.add_node('neuron0', **{
               'class': 'LeakyIAFwithRefractoryPeriod',
               'name': 'LeakyIAFwithRefractoryPeriod',
               'resting_potential': -70.0,
               'threshold': -45.0,
               'reset_potential': -55.0,
               'capacitance': 0.0744005237682, # in mS
               'resistance': 1.0, # in (k\Omega cm.^2)
               'refractory_period': .1, # in milliseconds
               'time_constant': 16.0, # in milliseconds
               'bias_current': 0.0
               })


    comp_dict, conns = LPU.graph_to_dicts(G)

    # use a input processor that present a step current (I) input to 'neuron0'
    # the step is from 0.2 to 0.8 and the step height is 10.0
    fl_input_processor = StepInputProcessor('I', ['neuron0'], 10.0, 0.2, 0.8)
    # output processor to record 'spike_state' and 'V' to hdf5 file 'new_output.h5',
    # with a sampling interval of 1 run step.
    fl_output_processor = FileOutputProcessor([('spike_state', None),('V', None)], 'new_output.h5', sample_interval=1)

    man.add(LPU, 'ge', dt, comp_dict, conns,
            device=args.gpu_dev, input_processors = [fl_input_processor],
            output_processors = [fl_output_processor], debug=args.debug)

    man.spawn()
    man.start(steps=args.steps)
    man.wait()

    import h5py
    import matplotlib
    matplotlib.use('PS')
    import matplotlib.pyplot as plt

    f = h5py.File('new_output.h5')
    t = np.arange(0, args.steps)*dt

    plt.figure()
    plt.plot(t,list(f['V'].values())[0])
    plt.xlabel('time, [s]')
    plt.ylabel('Voltage, [mV]')
    plt.title('Leaky Integrate-and-Fire Neuron with Refractory Period')
    plt.savefig('lifrp.png',dpi=300)
