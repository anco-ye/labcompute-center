"""Preserve a task's declared alternative adapter after a native OS mismatch.

Pure policy helper for an administrator's locked recovery transaction. The caller
must prove execution stopped, startup failed, and no successful/active/cancelled
successor exists. This helper never submits work or inspects scientific outputs.
"""
import copy


def exclude_platform(spec, platform, evidence):
    adapters=spec.get('platforms',{})
    if platform not in adapters or len(adapters)<2:raise ValueError('No declared alternative adapter')
    revised=copy.deepcopy(spec);revised['platforms'].pop(platform)
    target=spec.get('os')
    if target not in revised['platforms']:target=next(iter(revised['platforms']))
    adapter=revised['platforms'][target]
    revised['os']=target
    for key in ('runtime','entry','args','requirements'):
        if key in adapter:revised[key]=copy.deepcopy(adapter[key])
    revised.setdefault('platform_exclusions',{})[platform]=copy.deepcopy(evidence)
    for key in ('selected_platform','runtime_revision','cpu_ids','gpu_ids','work_volume','work_layout_ready','runtime_disk_gb','shared_node','deadline','backfill_deadline'):
        revised.pop(key,None)
    return revised
