"""Task-local policy; never activates in Agent, Broker, Web or operator processes."""
import os
if os.environ.get('LAB_TASK_NETWORK_POLICY') == 'bounded-v1':
    from lab_network_policy import install
    install()
    from lab_vector_compat import enable_on_neuron_import
    enable_on_neuron_import()
