from brian2 import *

# set_device('cpp_standalone', directory='brian2_CUBA')
prefs.codegen.target = "cython"

taum = 20 * ms
taue = 5 * ms
taui = 10 * ms
Vt = -50 * mV
Vr = -60 * mV
El = -49 * mV

eqs = '''
dv/dt  = (ge+gi-(v-El))/taum : volt (unless refractory)
dge/dt = -ge/taue : volt
dgi/dt = -gi/taui : volt
'''

P = NeuronGroup(4000, eqs, threshold='v>Vt', reset='v = Vr',
                refractory=5 * ms, method='euler')
P.v = 'Vr + rand() * (Vt - Vr)'
P.ge = 0 * mV
P.gi = 0 * mV

we = (60 * 0.27 / 10) * mV  # excitatory synaptic weight (voltage)
Ce = Synapses(P, P, on_pre='ge += we')
Ce.connect('i<3200', p=0.02)
wi = (-20 * 4.5 / 10) * mV  # inhibitory synaptic weight
Ci = Synapses(P, P, on_pre='gi += wi')
Ci.connect('i>=3200', p=0.02)

s_mon = SpikeMonitor(P)

t0 = time.time()
run(5 * second)
print('{}. Used time {} s.'.format(prefs.codegen.target, time.time() - t0))

plot(s_mon.t / ms, s_mon.i, ',k')
xlabel('Time (ms)')
ylabel('Neuron index')
show()
