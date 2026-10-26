"""Explicit platform variants; one task identity and one eventual execution lease."""
import hashlib
import json
import re


def normalize(raw, base, files, archive):
    variants=raw.get('platforms')
    if variants is None:return None
    if not isinstance(variants,dict) or not variants or set(variants)-{'windows','linux'}:
        raise ValueError('platforms 必须按 windows/linux 声明受支持的执行入口')
    from common import task_spec,relative_path
    out={}
    for platform,variant in variants.items():
        if not isinstance(variant,dict) or set(variant)-{'entry','runtime','args','requirements','min_version'}:
            raise ValueError('平台适配仅允许 entry/runtime/args/requirements/min_version，不得覆盖资源或身份')
        if 'runtime' not in variant:raise ValueError('每个平台必须明确声明已发布 runtime')
        if base['kind']=='comsol' and 'min_version' not in variant:
            raise ValueError('COMSOL 平台适配必须声明模型所需 min_version')
        parsed=task_spec(dict(kind=base['kind'],entry=variant.get('entry',base['entry']),
            runtime=variant['runtime'],args=variant.get('args',base['args']),os=platform),files)
        requirement=variant.get('requirements',base.get('requirements'))
        if requirement:
            requirement=relative_path(requirement)
            if base['kind']!='python' or requirement not in files or archive.getinfo(requirement).file_size>1024**2:
                raise ValueError('平台依赖文件无效')
        item={k:parsed[k] for k in ('entry','runtime','args')};item['requirements']=requirement
        if 'min_version' in variant:
            version=variant['min_version']
            if not isinstance(version,str) or not re.fullmatch(r'\d+(?:\.\d+){0,3}',version):
                raise ValueError('min_version 必须为数字版本，例如 6.4.0.293')
            item['min_application_version']=version
        out[platform]=item
    return out


def resolve(request, platform):
    variants=request.get('platforms')
    if not variants:return request
    variant=variants.get(platform)
    if variant is None:return None
    result=dict(request)
    result.pop('min_application_version',None)
    result.update(variant,os=platform,selected_platform=platform)
    # Runtime histories remain distinct even when both variants share input files.
    profile=request.get('portable_profile',request.get('profile'))
    if profile:
        result['portable_profile']=profile
        result['profile']=hashlib.sha256(json.dumps([profile,platform,variant],sort_keys=True).encode()).hexdigest()
    return result


def version_ready(request,runtime):
    minimum=request.get('min_application_version')
    if not minimum:return True
    version=runtime.get('version')
    if not isinstance(version,str):return False
    match=re.search(r'\d+(?:\.\d+){0,3}',version)
    if not match:return False
    def parts(s):return tuple((list(map(int,s.split('.')))+[0]*4)[:4])
    return parts(match[0])>=parts(minimum)
