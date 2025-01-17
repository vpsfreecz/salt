# -*- coding: utf-8 -*-
'''
Install Salt on an LXC Container
================================

.. versionadded:: 2014.7.0

Please read :ref:`core config documentation <config_lxc>`.
'''

# Import python libs
from __future__ import absolute_import
import json
import os
import logging
import copy
import time
from pprint import pformat

# Import salt libs
import salt.utils

# Import salt cloud libs
import salt.utils.cloud
import salt.config as config
from salt.exceptions import SaltCloudSystemExit

import salt.client
import salt.runner


# Import 3rd-party libs
import salt.ext.six as six

# Get logging started
log = logging.getLogger(__name__)

__FUN_TIMEOUT = {
    'cmd.run': 60 * 60,
    'test.ping': 10,
    'lxc.info': 40,
    'lxc.list': 300,
    'lxc.templates': 100,
    'grains.items': 100,
}
__CACHED_CALLS = {}
__CACHED_FUNS = {
    'test.ping': 3 * 60,  # cache ping for 3 minutes
    'lxc.list':  2  # cache lxc.list for 2 seconds
}


def __virtual__():
    '''
    Needs no special configuration
    '''
    return True


def _get_grain_id(id_):
    if not get_configured_provider():
        return
    infos = get_configured_provider()
    return 'salt.cloud.lxc.{0}.{1}'.format(infos['target'], id_)


def _minion_opts(cfg='minion'):
    if 'conf_file' in __opts__:
        default_dir = os.path.dirname(__opts__['conf_file'])
    else:
        default_dir = __opts__['config_dir'],
    cfg = os.environ.get(
        'SALT_MINION_CONFIG', os.path.join(default_dir, cfg))
    opts = config.minion_config(cfg)
    return opts


def _master_opts(cfg='master'):
    cfg = os.environ.get(
        'SALT_MASTER_CONFIG',
        __opts__.get('conf_file',
                     os.path.join(__opts__['config_dir'], cfg)))
    opts = config.master_config(cfg)
    return opts


def _client():
    return salt.client.get_local_client(mopts=_master_opts())


def _runner():
    # opts = _master_opts()
    # opts['output'] = 'quiet'
    return salt.runner.RunnerClient(_master_opts())


def _salt(fun, *args, **kw):
    '''Execute a salt function on a specific minion

    Special kwargs:

            salt_target
                target to exec things on
            salt_timeout
                timeout for jobs
            salt_job_poll
                poll interval to wait for job finish result
    '''
    try:
        poll = kw.pop('salt_job_poll')
    except KeyError:
        poll = 0.1
    try:
        target = kw.pop('salt_target')
    except KeyError:
        target = None
    try:
        timeout = int(kw.pop('salt_timeout'))
    except (KeyError, ValueError):
        # try to has some low timeouts for very basic commands
        timeout = __FUN_TIMEOUT.get(
            fun,
            900  # wait up to 15 minutes for the default timeout
        )
    try:
        kwargs = kw.pop('kwargs')
    except KeyError:
        kwargs = {}
    if not target:
        infos = get_configured_provider()
        if not infos:
            return
        target = infos['target']
    laps = time.time()
    cache = False
    if fun in __CACHED_FUNS:
        cache = True
        laps = laps // __CACHED_FUNS[fun]
    try:
        sargs = json.dumps(args)
    except TypeError:
        sargs = ''
    try:
        skw = json.dumps(kw)
    except TypeError:
        skw = ''
    try:
        skwargs = json.dumps(kwargs)
    except TypeError:
        skwargs = ''
    cache_key = (laps, target, fun, sargs, skw, skwargs)
    if not cache or (cache and (cache_key not in __CACHED_CALLS)):
        conn = _client()
        runner = _runner()
        rkwargs = kwargs.copy()
        rkwargs['timeout'] = timeout
        rkwargs.setdefault('expr_form', 'list')
        kwargs.setdefault('expr_form', 'list')
        ping_retries = 0
        # the target(s) have environ one minute to respond
        # we call 60 ping request, this prevent us
        # from blindly send commands to unmatched minions
        ping_max_retries = 60
        ping = True
        # do not check ping... if we are pinguing
        if fun == 'test.ping':
            ping_retries = ping_max_retries + 1
        # be sure that the executors are alive
        while ping_retries <= ping_max_retries:
            try:
                if ping_retries > 0:
                    time.sleep(1)
                pings = conn.cmd(tgt=target,
                                 timeout=10,
                                 fun='test.ping')
                values = list(pings.values())
                if not values:
                    ping = False
                for v in values:
                    if v is not True:
                        ping = False
                if not ping:
                    raise ValueError('Unreachable')
                break
            except Exception:
                ping = False
                ping_retries += 1
                log.error('{0} unreachable, retrying'.format(target))
        if not ping:
            raise SaltCloudSystemExit('Target {0} unreachable'.format(target))
        jid = conn.cmd_async(tgt=target,
                             fun=fun,
                             arg=args,
                             kwarg=kw,
                             **rkwargs)
        cret = conn.cmd(tgt=target,
                        fun='saltutil.find_job',
                        arg=[jid],
                        timeout=10,
                        **kwargs)
        running = bool(cret.get(target, False))
        endto = time.time() + timeout
        while running:
            rkwargs = {
                'tgt': target,
                'fun': 'saltutil.find_job',
                'arg': [jid],
                'timeout': 10
            }
            cret = conn.cmd(**rkwargs)
            running = bool(cret.get(target, False))
            if not running:
                break
            if running and (time.time() > endto):
                raise Exception('Timeout {0}s for {1} is elapsed'.format(
                    timeout, pformat(rkwargs)))
            time.sleep(poll)
        # timeout for the master to return data about a specific job
        wait_for_res = float({
            'test.ping': '5',
        }.get(fun, '120'))
        while wait_for_res:
            wait_for_res -= 0.5
            cret = runner.cmd(
                'jobs.lookup_jid',
                [jid, {'__kwarg__': True}])
            if target in cret:
                ret = cret[target]
                break
            # recent changes
            elif 'data' in cret and 'outputter' in cret:
                ret = cret['data']
                break
            # special case, some answers may be crafted
            # to handle the unresponsivness of a specific command
            # which is also meaningful, e.g. a minion not yet provisioned
            if fun in ['test.ping'] and not wait_for_res:
                ret = {
                    'test.ping': False,
                }.get(fun, False)
            time.sleep(0.5)
        try:
            if 'is not available.' in ret:
                raise SaltCloudSystemExit(
                    'module/function {0} is not available'.format(fun))
        except SaltCloudSystemExit:
            raise
        except TypeError:
            pass
        if cache:
            __CACHED_CALLS[cache_key] = ret
    elif cache and cache_key in __CACHED_CALLS:
        ret = __CACHED_CALLS[cache_key]
    return ret


def avail_images():
    return _salt('lxc.templates')


def list_nodes(conn=None, call=None):
    hide = False
    names = __opts__.get('names', [])
    profiles = __opts__.get('profiles', {})
    profile = __opts__.get('profile',
                           __opts__.get('internal_lxc_profile', []))
    destroy_opt = __opts__.get('destroy', False)
    action = __opts__.get('action', '')
    for opt in ['full_query', 'select_query', 'query']:
        if __opts__.get(opt, False):
            call = 'full'
    if destroy_opt:
        call = 'full'
    if action and not call:
        call = 'action'
    if profile and names and not destroy_opt:
        hide = True
    if not get_configured_provider():
        return

    path = None
    if profile and profile in profiles:
        path = profiles[profile].get('path', None)
    lxclist = _salt('lxc.list', extra=True, path=path)
    nodes = {}
    for state, lxcs in six.iteritems(lxclist):
        for lxcc, linfos in six.iteritems(lxcs):
            info = {
                'id': lxcc,
                'name': lxcc,  # required for cloud cache
                'image': None,
                'size': linfos['size'],
                'state': state.lower(),
                'public_ips': linfos['public_ips'],
                'private_ips': linfos['private_ips'],
            }
            # in creation mode, we need to go inside the create method
            # so we hide the running vm from being seen as already installed
            # do not also mask half configured nodes which are explicitly asked
            # to be acted on, on the command line
            if (call in ['full'] or not hide) and ((lxcc in names and call in ['action']) or call in ['full']):
                nodes[lxcc] = {
                    'id': lxcc,
                    'name': lxcc,  # required for cloud cache
                    'image': None,
                    'size': linfos['size'],
                    'state': state.lower(),
                    'public_ips': linfos['public_ips'],
                    'private_ips': linfos['private_ips'],
                }
            else:
                nodes[lxcc] = {'id': lxcc, 'state': state.lower()}
    return nodes


def list_nodes_full(conn=None, call=None):
    if not get_configured_provider():
        return
    if not call:
        call = 'action'
    return list_nodes(conn=conn, call=call)


def show_instance(name, call=None):
    '''
    Show the details from the provider concerning an instance
    '''

    if not get_configured_provider():
        return
    if not call:
        call = 'action'
    nodes = list_nodes_full(call=call)
    __utils__['cloud.cache_node'](nodes[name], __active_provider_name__, __opts__)
    return nodes[name]


def list_nodes_select(call=None):
    '''
    Return a list of the VMs that are on the provider, with select fields
    '''
    if not call:
        call = 'select'
    if not get_configured_provider():
        return
    info = ['id', 'name', 'image', 'size', 'state', 'public_ips', 'private_ips']
    return salt.utils.cloud.list_nodes_select(
        list_nodes_full(call='action'),
        __opts__.get('query.selection', info), call)


def _checkpoint(ret):
    sret = '''
id: {name}
last message: {comment}'''.format(**ret)
    keys = list(ret['changes'].items())
    keys.sort()
    for ch, comment in keys:
        sret += (
            '\n'
            '    {0}:\n'
            '      {1}'
        ).format(ch, comment.replace(
            '\n',
            '\n'
            '      '))
    if not ret['result']:
        if 'changes' in ret:
            del ret['changes']
        raise SaltCloudSystemExit(sret)
    log.info(sret)
    return sret


def destroy(vm_, call=None):
    '''Destroy a lxc container'''
    destroy_opt = __opts__.get('destroy', False)
    profiles = __opts__.get('profiles', {})
    profile = __opts__.get('profile',
                           __opts__.get('internal_lxc_profile', []))
    path = None
    if profile and profile in profiles:
        path = profiles[profile].get('path', None)
    action = __opts__.get('action', '')
    if action != 'destroy' and not destroy_opt:
        raise SaltCloudSystemExit(
            'The destroy action must be called with -d, --destroy, '
            '-a or --action.'
        )
    if not get_configured_provider():
        return
    ret = {'comment': '{0} was not found'.format(vm_),
           'result': False}
    if _salt('lxc.info', vm_, path=path):
        __utils__['cloud.fire_event'](
            'event',
            'destroying instance',
            'salt/cloud/{0}/destroying'.format(vm_),
            {'name': vm_, 'instance_id': vm_},
            transport=__opts__['transport']
        )
        cret = _salt('lxc.destroy', vm_, stop=True, path=path)
        ret['result'] = cret['result']
        if ret['result']:
            ret['comment'] = '{0} was destroyed'.format(vm_)
            __utils__['cloud.fire_event'](
                'event',
                'destroyed instance',
                'salt/cloud/{0}/destroyed'.format(vm_),
                {'name': vm_, 'instance_id': vm_},
                transport=__opts__['transport']
            )
            if __opts__.get('update_cachedir', False) is True:
                __utils__['cloud.delete_minion_cachedir'](vm_, __active_provider_name__.split(':')[0], __opts__)
    return ret


def create(vm_, call=None):
    '''Create an lxc Container.
    This function is idempotent and will try to either provision
    or finish the provision of an lxc container.

    NOTE: Most of the initialization code has been moved and merged
    with the lxc runner and lxc.init functions
    '''
    prov = get_configured_provider(vm_)
    if not prov:
        return
    # we cant use profile as a configuration key as it conflicts
    # with salt cloud internals
    profile = vm_.get(
        'lxc_profile',
        vm_.get('container_profile', None))

    # Since using "provider: <provider-engine>" is deprecated, alias provider
    # to use driver: "driver: <provider-engine>"
    if 'provider' in vm_:
        vm_['driver'] = vm_.pop('provider')

    __utils__['cloud.fire_event'](
        'event', 'starting create',
        'salt/cloud/{0}/creating'.format(vm_['name']),
        {'name': vm_['name'], 'profile': profile,
         'provider': vm_['driver'], },
        transport=__opts__['transport'])
    ret = {'name': vm_['name'], 'changes': {}, 'result': True, 'comment': ''}
    if 'pub_key' not in vm_ and 'priv_key' not in vm_:
        log.debug('Generating minion keys for {0}'.format(vm_['name']))
        vm_['priv_key'], vm_['pub_key'] = salt.utils.cloud.gen_keys(
            salt.config.get_cloud_config_value(
                'keysize', vm_, __opts__))
    # get the minion key pair to distribute back to the container
    kwarg = copy.deepcopy(vm_)
    kwarg['host'] = prov['target']
    kwarg['profile'] = profile
    cret = _runner().cmd('lxc.cloud_init', [vm_['name']], kwarg=kwarg)
    ret['runner_return'] = cret
    ret['result'] = cret['result']
    if not ret['result']:
        ret['Error'] = 'Error while creating {0},'.format(vm_['name'])
    else:
        ret['changes']['created'] = 'created'

    # When using cloud states to manage LXC containers
    # __opts__['profile'] is not implicitly reset between operations
    # on different containers. However list_nodes will hide container
    # if profile is set in opts assuming that it have to be created.
    # But in cloud state we do want to check at first if it really
    # exists hence the need to remove profile from global opts once
    # current container is created.
    if 'profile' in __opts__:
        __opts__['internal_lxc_profile'] = __opts__['profile']
        del __opts__['profile']

    __utils__['cloud.fire_event'](
        'event',
        'created instance',
        'salt/cloud/{0}/created'.format(vm_['name']),
        {
            'name': vm_['name'],
            'profile': vm_['profile'],
            'provider': vm_['driver'],
        },
        transport=__opts__['transport']
    )

    return ret


def get_provider(name):
    data = None
    if name in __opts__['providers']:
        data = __opts__['providers'][name]
        if 'lxc' in data:
            data = data['lxc']
        else:
            data = None
    return data


def get_configured_provider(vm_=None):
    '''
    Return the contextual provider of None if no configured
    one can be found.
    '''
    if vm_ is None:
        vm_ = {}
    dalias, driver = __active_provider_name__.split(':')
    data = None
    tgt = 'unknown'
    img_provider = __opts__.get('list_images', '')
    arg_providers = __opts__.get('names', [])
    matched = False
    # --list-images level
    if img_provider:
        tgt = 'provider: {0}'.format(img_provider)
        if dalias == img_provider:
            data = get_provider(img_provider)
            matched = True
    # providers are set in configuration
    if not data and 'profile' not in __opts__ and arg_providers:
        for name in arg_providers:
            tgt = 'provider: {0}'.format(name)
            if dalias == name:
                data = get_provider(name)
            if data:
                matched = True
                break
    # -p is providen, get the uplinked provider
    elif 'profile' in __opts__:
        curprof = __opts__['profile']
        profs = __opts__['profiles']
        tgt = 'profile: {0}'.format(curprof)
        if (
            curprof in profs and
            profs[curprof]['provider'] == __active_provider_name__
        ):
            prov, cdriver = profs[curprof]['provider'].split(':')
            tgt += ' provider: {0}'.format(prov)
            data = get_provider(prov)
            matched = True
    # fallback if we have only __active_provider_name__
    if (
        (__opts__.get('destroy', False) and not data) or (
            not matched and __active_provider_name__
        )
    ):
        data = __opts__.get('providers',
                            {}).get(dalias, {}).get(driver, {})
    # in all cases, verify that the linked saltmaster is alive.
    if data:
        ret = _salt('test.ping', salt_target=data['target'])
        if not ret:
            raise SaltCloudSystemExit(
                'Configured provider {0} minion: {1} is unreachable'.format(
                    __active_provider_name__, data['target']))
        return data
    return False
