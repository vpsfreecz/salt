# -*- coding: utf-8 -*-
'''
The Saltutil module is used to manage the state of the salt minion itself. It
is used to manage minion modules as well as automate updates to the salt
minion.

:depends:   - esky Python module for update functionality
'''
from __future__ import absolute_import

# Import python libs
import os
import shutil
import signal
import logging
import fnmatch
import sys
import copy

# Import 3rd-party libs
# pylint: disable=import-error
try:
    import esky
    from esky import EskyVersionError
    HAS_ESKY = True
except ImportError:
    HAS_ESKY = False
# pylint: disable=no-name-in-module
import salt.ext.six as six
from salt.ext.six.moves.urllib.error import URLError
# pylint: enable=import-error,no-name-in-module

# Fix a nasty bug with Win32 Python not supporting all of the standard signals
try:
    salt_SIGKILL = signal.SIGKILL
except AttributeError:
    salt_SIGKILL = signal.SIGTERM

# Import salt libs
import salt
import salt.payload
import salt.state
import salt.client
import salt.client.ssh.client
import salt.config
import salt.runner
import salt.utils
import salt.utils.args
import salt.utils.process
import salt.utils.minion
import salt.utils.event
import salt.utils.url
import salt.transport
import salt.wheel

HAS_PSUTIL = True
try:
    import salt.utils.psutil_compat
except ImportError:
    HAS_PSUTIL = False

from salt.exceptions import (
    SaltReqTimeoutError, SaltRenderError, CommandExecutionError, SaltInvocationError
)

__proxyenabled__ = ['*']

log = logging.getLogger(__name__)


def _get_top_file_envs():
    '''
    Get all environments from the top file
    '''
    try:
        return __context__['saltutil._top_file_envs']
    except KeyError:
        try:
            st_ = salt.state.HighState(__opts__)
            top = st_.get_top()
            if top:
                envs = list(st_.top_matches(top).keys()) or 'base'
            else:
                envs = 'base'
        except SaltRenderError as exc:
            raise CommandExecutionError(
                'Unable to render top file(s): {0}'.format(exc)
            )
        __context__['saltutil._top_file_envs'] = envs
        return envs


def _sync(form, saltenv=None):
    '''
    Sync the given directory in the given environment
    '''
    if saltenv is None:
        saltenv = _get_top_file_envs()
    if isinstance(saltenv, six.string_types):
        saltenv = saltenv.split(',')
    ret = []
    remote = set()
    source = salt.utils.url.create('_' + form)
    mod_dir = os.path.join(__opts__['extension_modules'], '{0}'.format(form))
    cumask = os.umask(0o77)
    if not os.path.isdir(mod_dir):
        log.info('Creating module dir {0!r}'.format(mod_dir))
        try:
            os.makedirs(mod_dir)
        except (IOError, OSError):
            msg = 'Cannot create cache module directory {0}. Check permissions.'
            log.error(msg.format(mod_dir))
    for sub_env in saltenv:
        log.info('Syncing {0} for environment {1!r}'.format(form, sub_env))
        cache = []
        log.info('Loading cache from {0}, for {1})'.format(source, sub_env))
        # Grab only the desired files (.py, .pyx, .so)
        cache.extend(
            __salt__['cp.cache_dir'](
                source, sub_env, include_pat=r'E@\.(pyx?|so|zip)$'
            )
        )
        local_cache_dir = os.path.join(
                __opts__['cachedir'],
                'files',
                sub_env,
                '_{0}'.format(form)
                )
        log.debug('Local cache dir: {0!r}'.format(local_cache_dir))
        for fn_ in cache:
            relpath = os.path.relpath(fn_, local_cache_dir)
            relname = os.path.splitext(relpath)[0].replace(os.sep, '.')
            remote.add(relpath)
            dest = os.path.join(mod_dir, relpath)
            log.info('Copying {0!r} to {1!r}'.format(fn_, dest))
            if os.path.isfile(dest):
                # The file is present, if the sum differs replace it
                hash_type = __opts__.get('hash_type', 'md5')
                src_digest = salt.utils.get_hash(fn_, hash_type)
                dst_digest = salt.utils.get_hash(dest, hash_type)
                if src_digest != dst_digest:
                    # The downloaded file differs, replace!
                    shutil.copyfile(fn_, dest)
                    ret.append('{0}.{1}'.format(form, relname))
            else:
                dest_dir = os.path.dirname(dest)
                if not os.path.isdir(dest_dir):
                    os.makedirs(dest_dir)
                shutil.copyfile(fn_, dest)
                ret.append('{0}.{1}'.format(form, relname))

    touched = bool(ret)
    if __opts__.get('clean_dynamic_modules', True):
        current = set(_listdir_recursively(mod_dir))
        for fn_ in current - remote:
            full = os.path.join(mod_dir, fn_)
            if os.path.isfile(full):
                touched = True
                os.remove(full)
        # Cleanup empty dirs
        while True:
            emptydirs = _list_emptydirs(mod_dir)
            if not emptydirs:
                break
            for emptydir in emptydirs:
                touched = True
                shutil.rmtree(emptydir, ignore_errors=True)
    # Dest mod_dir is touched? trigger reload if requested
    if touched:
        mod_file = os.path.join(__opts__['cachedir'], 'module_refresh')
        with salt.utils.fopen(mod_file, 'a+') as ofile:
            ofile.write('')
    if form == 'grains' and \
       __opts__.get('grains_cache') and \
       os.path.isfile(os.path.join(__opts__['cachedir'], 'grains.cache.p')):
        try:
            os.remove(os.path.join(__opts__['cachedir'], 'grains.cache.p'))
        except OSError:
            log.error('Could not remove grains cache!')
    os.umask(cumask)
    return ret


def _listdir_recursively(rootdir):
    file_list = []
    for root, dirs, files in os.walk(rootdir):
        for filename in files:
            relpath = os.path.relpath(root, rootdir).strip('.')
            file_list.append(os.path.join(relpath, filename))
    return file_list


def _list_emptydirs(rootdir):
    emptydirs = []
    for root, dirs, files in os.walk(rootdir):
        if not files and not dirs:
            emptydirs.append(root)
    return emptydirs


def update(version=None):
    '''
    Update the salt minion from the URL defined in opts['update_url']
    SaltStack, Inc provides the latest builds here:
    update_url: https://repo.saltstack.com/windows/

    Be aware that as of 2014-8-11 there's a bug in esky such that only the
    latest version available in the update_url can be downloaded and installed.

    This feature requires the minion to be running a bdist_esky build.

    The version number is optional and will default to the most recent version
    available at opts['update_url'].

    Returns details about the transaction upon completion.

    CLI Example:

    .. code-block:: bash

        salt '*' saltutil.update
        salt '*' saltutil.update 0.10.3
    '''
    ret = {}
    if not HAS_ESKY:
        ret['_error'] = 'Esky not available as import'
        return ret
    if not getattr(sys, 'frozen', False):
        ret['_error'] = 'Minion is not running an Esky build'
        return ret
    if not __salt__['config.option']('update_url'):
        ret['_error'] = '"update_url" not configured on this minion'
        return ret
    app = esky.Esky(sys.executable, __opts__['update_url'])
    oldversion = __grains__['saltversion']
    if not version:
        try:
            version = app.find_update()
        except URLError as exc:
            ret['_error'] = 'Could not connect to update_url. Error: {0}'.format(exc)
            return ret
    if not version:
        ret['_error'] = 'No updates available'
        return ret
    try:
        app.fetch_version(version)
    except EskyVersionError as exc:
        ret['_error'] = 'Unable to fetch version {0}. Error: {1}'.format(version, exc)
        return ret
    try:
        app.install_version(version)
    except EskyVersionError as exc:
        ret['_error'] = 'Unable to install version {0}. Error: {1}'.format(version, exc)
        return ret
    try:
        app.cleanup()
    except Exception as exc:
        ret['_error'] = 'Unable to cleanup. Error: {0}'.format(exc)
    restarted = {}
    for service in __opts__['update_restart_services']:
        restarted[service] = __salt__['service.restart'](service)
    ret['comment'] = 'Updated from {0} to {1}'.format(oldversion, version)
    ret['restarted'] = restarted
    return ret


def sync_beacons(saltenv=None, refresh=True):
    '''
    .. versionadded:: 2015.5.1

    Sync the beacons from the ``salt://_beacons`` directory on the Salt
    fileserver. This function is environment-aware, pass the desired
    environment to grab the contents of the ``_beacons`` directory from that
    environment. The default environment, if none is specified,  is ``base``.

    refresh : True
        Also refresh the beacons available to the minion.

    CLI Example:

    .. code-block:: bash

        salt '*' saltutil.sync_beacons
        salt '*' saltutil.sync_beacons saltenv=dev
    '''
    ret = _sync('beacons', saltenv)
    if refresh:
        refresh_beacons()
    return ret


def sync_sdb(saltenv=None, refresh=False):
    '''
    .. versionadded:: 2015.5.7

    Sync sdb modules from the ``salt://_sdb`` directory on the Salt
    fileserver. This function is environment-aware, pass the desired
    environment to grab the contents of the ``_sdb`` directory from that
    environment. The default environment, if none is specified,  is ``base``.

    refresh : False
        This argument has no affect and is included for consistency with the
        other sync functions.

    CLI Example:

    .. code-block:: bash

        salt '*' saltutil.sync_sdb
        salt '*' saltutil.sync_sdb saltenv=dev
    '''
    ret = _sync('sdb', saltenv)
    return ret


def sync_modules(saltenv=None, refresh=True):
    '''
    Sync the modules from the ``salt://_modules`` directory on the Salt
    fileserver. This function is environment-aware, pass the desired
    environment to grab the contents of the ``_modules`` directory from that
    environment. The default environment, if none is specified,  is ``base``.

    refresh : True
        Also refresh the execution modules available to the minion.

    .. important::

        If this function is executed using a :py:func:`module.run
        <salt.states.module.run>` state, the SLS file will not have access to
        newly synced execution modules unless a ``refresh`` argument is
        added to the state, like so:

        .. code-block:: yaml

            load_my_custom_module:
              module.run:
                - name: saltutil.sync_modules
                - refresh: True

        See :ref:`here <reloading-modules>` for a more detailed explanation of
        why this is necessary.

    CLI Example:

    .. code-block:: bash

        salt '*' saltutil.sync_modules
        salt '*' saltutil.sync_modules saltenv=dev
    '''
    ret = _sync('modules', saltenv)
    if refresh:
        refresh_modules()
    return ret


def sync_states(saltenv=None, refresh=True):
    '''
    Sync the states from the ``salt://_states`` directory on the Salt
    fileserver. This function is environment-aware, pass the desired
    environment to grab the contents of the ``_states`` directory from that
    environment. The default environment, if none is specified,  is ``base``.

    refresh : True
        Also refresh the execution modules available to the minion.

    CLI Example:

    .. code-block:: bash

        salt '*' saltutil.sync_states
        salt '*' saltutil.sync_states saltenv=dev
    '''
    ret = _sync('states', saltenv)
    if refresh:
        refresh_modules()
    return ret


def sync_grains(saltenv=None, refresh=True):
    '''
    Sync the grains from the ``salt://_grains`` directory on the Salt
    fileserver. This function is environment-aware, pass the desired
    environment to grab the contents of the ``_grains`` directory from that
    environment. The default environment, if none is specified,  is ``base``.

    refresh : True
        Also refresh the execution modules available to the minion, and refresh
        pillar data.

    CLI Example:

    .. code-block:: bash

        salt '*' saltutil.sync_grains
        salt '*' saltutil.sync_grains saltenv=dev
    '''
    ret = _sync('grains', saltenv)
    if refresh:
        refresh_modules()
        refresh_pillar()
    return ret


def sync_renderers(saltenv=None, refresh=True):
    '''
    Sync the renderers from the ``salt://_renderers`` directory on the Salt
    fileserver. This function is environment-aware, pass the desired
    environment to grab the contents of the ``_renderers`` directory from that
    environment. The default environment, if none is specified,  is ``base``.

    refresh : True
        Also refresh the execution modules available to the minion.

    CLI Example:

    .. code-block:: bash

        salt '*' saltutil.sync_renderers
        salt '*' saltutil.sync_renderers saltenv=dev
    '''
    ret = _sync('renderers', saltenv)
    if refresh:
        refresh_modules()
    return ret


def sync_returners(saltenv=None, refresh=True):
    '''
    Sync the returners from the ``salt://_returners`` directory on the Salt
    fileserver. This function is environment-aware, pass the desired
    environment to grab the contents of the ``_returners`` directory from that
    environment. The default environment, if none is specified,  is ``base``.

    refresh : True
        Also refresh the execution modules available to the minion.

    CLI Example:

    .. code-block:: bash

        salt '*' saltutil.sync_returners
        salt '*' saltutil.sync_returners saltenv=dev
    '''
    ret = _sync('returners', saltenv)
    if refresh:
        refresh_modules()
    return ret


def sync_proxymodules(saltenv=None, refresh=False):
    '''
    .. versionadded:: 2015.8.2

    Sync the proxy modules from the ``salt://_proxy`` directory on the Salt
    fileserver. This function is environment-aware, pass the desired
    environment to grab the contents of the ``_proxy`` directory from that
    environment. The default environment, if none is specified,  is ``base``.

    CLI Example:

    .. code-block:: bash

        salt '*' saltutil.sync_proxymodules
        salt '*' saltutil.sync_proxymodules saltenv=dev
    '''
    ret = _sync('proxy', saltenv)
    if refresh:
        refresh_modules()
    return ret


def sync_output(saltenv=None, refresh=True):
    '''
    Sync the output modules from the ``salt://_output`` directory on the Salt
    fileserver. This function is environment-aware, pass the desired
    environment to grab the contents of the ``_output`` directory from that
    environment. The default environment, if none is specified, is ``base``.

    refresh : True
        Also refresh the execution modules available to the minion.

    CLI Example:

    .. code-block:: bash

        salt '*' saltutil.sync_output
        salt '*' saltutil.sync_output saltenv=dev
    '''
    ret = _sync('output', saltenv)
    if refresh:
        refresh_modules()
    return ret

sync_outputters = salt.utils.alias_function(sync_output, 'sync_outputters')


def sync_utils(saltenv=None, refresh=True):
    '''
    .. versionadded:: 2014.7.0

    Sync utility source files from the ``salt://_utils`` directory on the Salt
    fileserver. This function is environment-aware, pass the desired
    environment to grab the contents of the ``_utils`` directory from that
    environment. The default environment, if none is specified, is ``base``.

    refresh : True
        Also refresh the execution modules available to the minion.

    CLI Example:

    .. code-block:: bash

        salt '*' saltutil.sync_utils
        salt '*' saltutil.sync_utils saltenv=dev
    '''
    ret = _sync('utils', saltenv)
    if refresh:
        refresh_modules()
    return ret


def sync_log_handlers(saltenv=None, refresh=True):
    '''
    .. versionadded:: 2015.8.0

    Sync utility source files from the ``salt://_log_handlers`` directory on
    the Salt fileserver. This function is environment-aware, pass the desired
    environment to grab the contents of the ``_log_handlers`` directory from
    that environment. The default environment, if none is specified,  is
    ``base``.

    CLI Example:

    .. code-block:: bash

        salt '*' saltutil.sync_log_handlers
        salt '*' saltutil.sync_log_handlers saltenv=dev
    '''
    ret = _sync('log_handlers', saltenv)
    if refresh:
        refresh_modules()
    return ret


def sync_pillar(saltenv=None, refresh=True):
    '''
    .. versionadded:: 2015.8.11,2016.3.2

    Sync pillar modules from the ``salt://_pillar`` directory on the Salt
    fileserver. This function is environment-aware, pass the desired
    environment to grab the contents of the ``_pillar`` directory from that
    environment. The default environment, if none is specified,  is ``base``.

    refresh : True
        Also refresh the execution modules available to the minion, and refresh
        pillar data.

    .. note::
        This function will raise an error if executed on a traditional (i.e.
        not masterless) minion

    CLI Examples:

    .. code-block:: bash

        salt '*' saltutil.sync_pillar
        salt '*' saltutil.sync_pillar saltenv=dev
    '''
    if __opts__['file_client'] != 'local':
        raise CommandExecutionError(
            'Pillar modules can only be synced to masterless minions'
        )
    ret = _sync('pillar', saltenv)
    if refresh:
        refresh_modules()
        refresh_pillar()
    return ret


def sync_all(saltenv=None, refresh=True):
    '''
    .. versionchanged:: 2015.8.11,2016.3.2
        On masterless minions, pillar modules are now synced, and refreshed
        when ``refresh`` is set to ``True``.

    Sync down all of the dynamic modules from the file server for a specific
    environment. This function synchronizes custom modules, states, beacons,
    grains, returners, output modules, renderers, and utils.

    refresh : True
        Also refresh the execution modules available to the minion.

    .. important::

        If this function is executed using a :py:func:`module.run
        <salt.states.module.run>` state, the SLS file will not have access to
        newly synced execution modules unless a ``refresh`` argument is
        added to the state, like so:

        .. code-block:: yaml

            load_my_custom_module:
              module.run:
                - name: saltutil.sync_all
                - refresh: True

        See :ref:`here <reloading-modules>` for a more detailed explanation of
        why this is necessary.

    CLI Example:

    .. code-block:: bash

        salt '*' saltutil.sync_all
        salt '*' saltutil.sync_all saltenv=dev
    '''
    log.debug('Syncing all')
    ret = {}
    ret['beacons'] = sync_beacons(saltenv, False)
    ret['modules'] = sync_modules(saltenv, False)
    ret['states'] = sync_states(saltenv, False)
    ret['sdb'] = sync_sdb(saltenv, False)
    ret['grains'] = sync_grains(saltenv, False)
    ret['renderers'] = sync_renderers(saltenv, False)
    ret['returners'] = sync_returners(saltenv, False)
    ret['output'] = sync_output(saltenv, False)
    ret['utils'] = sync_utils(saltenv, False)
    ret['log_handlers'] = sync_log_handlers(saltenv, False)
    ret['proxymodules'] = sync_proxymodules(saltenv, False)
    if __opts__['file_client'] == 'local':
        ret['pillar'] = sync_pillar(saltenv, False)
    if refresh:
        refresh_modules()
        if __opts__['file_client'] == 'local':
            refresh_pillar()
    return ret


def refresh_beacons():
    '''
    Signal the minion to refresh the beacons.

    CLI Example:

    .. code-block:: bash

        salt '*' saltutil.refresh_beacons
    '''
    try:
        ret = __salt__['event.fire']({}, 'beacons_refresh')
    except KeyError:
        log.error('Event module not available. Module refresh failed.')
        ret = False  # Effectively a no-op, since we can't really return without an event system
    return ret


def refresh_pillar():
    '''
    Signal the minion to refresh the pillar data.

    CLI Example:

    .. code-block:: bash

        salt '*' saltutil.refresh_pillar
    '''
    try:
        ret = __salt__['event.fire']({}, 'pillar_refresh')
    except KeyError:
        log.error('Event module not available. Module refresh failed.')
        ret = False  # Effectively a no-op, since we can't really return without an event system
    return ret

pillar_refresh = salt.utils.alias_function(refresh_pillar, 'pillar_refresh')


def refresh_modules(async=True):
    '''
    Signal the minion to refresh the module and grain data

    The default is to refresh module asynchronously. To block
    until the module refresh is complete, set the 'async' flag
    to False.

    CLI Example:

    .. code-block:: bash

        salt '*' saltutil.refresh_modules
    '''
    try:
        if async:
            #  If we're going to block, first setup a listener
            ret = __salt__['event.fire']({}, 'module_refresh')
        else:
            eventer = salt.utils.event.get_event('minion', opts=__opts__, listen=True)
            ret = __salt__['event.fire']({'notify': True}, 'module_refresh')
            # Wait for the finish event to fire
            log.trace('refresh_modules waiting for module refresh to complete')
            # Blocks until we hear this event or until the timeout expires
            eventer.get_event(tag='/salt/minion/minion_mod_complete', wait=30)
    except KeyError:
        log.error('Event module not available. Module refresh failed.')
        ret = False  # Effectively a no-op, since we can't really return without an event system
    return ret


def is_running(fun):
    '''
    If the named function is running return the data associated with it/them.
    The argument can be a glob

    CLI Example:

    .. code-block:: bash

        salt '*' saltutil.is_running state.highstate
    '''
    run = running()
    ret = []
    for data in run:
        if fnmatch.fnmatch(data.get('fun', ''), fun):
            ret.append(data)
    return ret


def running():
    '''
    Return the data on all running salt processes on the minion

    CLI Example:

    .. code-block:: bash

        salt '*' saltutil.running
    '''
    return salt.utils.minion.running(__opts__)


def clear_cache():
    '''
    Forcibly removes all caches on a minion.

    .. versionadded:: 2014.7.0

    WARNING: The safest way to clear a minion cache is by first stopping
    the minion and then deleting the cache files before restarting it.

    CLI Example:

    .. code-block:: bash

        salt '*' saltutil.clear_cache
    '''
    for root, dirs, files in salt.utils.safe_walk(__opts__['cachedir'], followlinks=False):
        for name in files:
            try:
                os.remove(os.path.join(root, name))
            except OSError as exc:
                log.error('Attempt to clear cache with saltutil.clear_cache FAILED with: {0}'.format(exc))
                return False
    return True


def find_job(jid):
    '''
    Return the data for a specific job id that is currently running.

    jid
        The job id to search for and return data.

    CLI Example:

    .. code-block:: bash

        salt '*' saltutil.find_job <job id>

    Note that the find_job function only returns job information when the job is still running. If
    the job is currently running, the output looks something like this:

    .. code-block:: bash

        # salt my-minion saltutil.find_job 20160503150049487736
        my-minion:
            ----------
            arg:
                - 30
            fun:
                test.sleep
            jid:
                20160503150049487736
            pid:
                9601
            ret:
            tgt:
                my-minion
            tgt_type:
                glob
            user:
                root

    If the job has already completed, the job cannot be found and therefore the function returns
    an empty dictionary, which looks like this on the CLI:

    .. code-block:: bash

        # salt my-minion saltutil.find_job 20160503150049487736
        my-minion:
            ----------
    '''
    for data in running():
        if data['jid'] == jid:
            return data
    return {}


def find_cached_job(jid):
    '''
    Return the data for a specific cached job id

    CLI Example:

    .. code-block:: bash

        salt '*' saltutil.find_cached_job <job id>
    '''
    serial = salt.payload.Serial(__opts__)
    proc_dir = os.path.join(__opts__['cachedir'], 'minion_jobs')
    job_dir = os.path.join(proc_dir, str(jid))
    if not os.path.isdir(job_dir):
        if not __opts__.get('cache_jobs'):
            return ('Local jobs cache directory not found; you may need to'
                    ' enable cache_jobs on this minion')
        else:
            return 'Local jobs cache directory {0} not found'.format(job_dir)
    path = os.path.join(job_dir, 'return.p')
    with salt.utils.fopen(path, 'rb') as fp_:
        buf = fp_.read()
        fp_.close()
        if buf:
            try:
                data = serial.loads(buf)
            except NameError:
                # msgpack error in salt-ssh
                return
        else:
            return
    if not isinstance(data, dict):
        # Invalid serial object
        return
    return data


def signal_job(jid, sig):
    '''
    Sends a signal to the named salt job's process

    CLI Example:

    .. code-block:: bash

        salt '*' saltutil.signal_job <job id> 15
    '''
    if HAS_PSUTIL is False:
        log.warning('saltutil.signal job called, but psutil is not installed. '
                    'Install psutil to ensure more reliable and accurate PID '
                    'management.')
    for data in running():
        if data['jid'] == jid:
            try:
                if HAS_PSUTIL:
                    for proc in salt.utils.psutil_compat.Process(pid=data['pid']).children(recursive=True):
                        proc.send_signal(sig)
                os.kill(int(data['pid']), sig)
                if HAS_PSUTIL is False and 'child_pids' in data:
                    for pid in data['child_pids']:
                        os.kill(int(pid), sig)
                return 'Signal {0} sent to job {1} at pid {2}'.format(
                        int(sig),
                        jid,
                        data['pid']
                        )
            except OSError:
                path = os.path.join(__opts__['cachedir'], 'proc', str(jid))
                if os.path.isfile(path):
                    os.remove(path)
                return ('Job {0} was not running and job data has been '
                        ' cleaned up').format(jid)
    return ''


def term_job(jid):
    '''
    Sends a termination signal (SIGTERM 15) to the named salt job's process

    CLI Example:

    .. code-block:: bash

        salt '*' saltutil.term_job <job id>
    '''
    return signal_job(jid, signal.SIGTERM)


def kill_job(jid):
    '''
    Sends a kill signal (SIGKILL 9) to the named salt job's process

    CLI Example:

    .. code-block:: bash

        salt '*' saltutil.kill_job <job id>
    '''
    # Some OS's (Win32) don't have SIGKILL, so use salt_SIGKILL which is set to
    # an appropriate value for the operating system this is running on.
    return signal_job(jid, salt_SIGKILL)


def regen_keys():
    '''
    Used to regenerate the minion keys.

    CLI Example:

    .. code-block:: bash

        salt '*' saltutil.regen_keys
    '''
    for fn_ in os.listdir(__opts__['pki_dir']):
        path = os.path.join(__opts__['pki_dir'], fn_)
        try:
            os.remove(path)
        except os.error:
            pass
    # TODO: move this into a channel function? Or auth?
    # create a channel again, this will force the key regen
    channel = salt.transport.Channel.factory(__opts__)


def revoke_auth(preserve_minion_cache=False):
    '''
    The minion sends a request to the master to revoke its own key.
    Note that the minion session will be revoked and the minion may
    not be able to return the result of this command back to the master.

    If the 'preserve_minion_cache' flag is set to True, the master
    cache for this minion will not be removed.

    CLI Example:

    .. code-block:: bash

        salt '*' saltutil.revoke_auth
    '''
    channel = salt.transport.Channel.factory(__opts__)
    tok = channel.auth.gen_token('salt')
    load = {'cmd': 'revoke_auth',
            'id': __opts__['id'],
            'tok': tok,
            'preserve_minion_cache': preserve_minion_cache}

    try:
        return channel.send(load)
    except SaltReqTimeoutError:
        return False


def _get_ssh_or_api_client(cfgfile, ssh=False):
    if ssh:
        client = salt.client.ssh.client.SSHClient(cfgfile)
    else:
        client = salt.client.get_local_client(cfgfile)
    return client


def _exec(client, tgt, fun, arg, timeout, expr_form, ret, kwarg, **kwargs):
    fcn_ret = {}
    seen = 0
    for ret_comp in client.cmd_iter(
            tgt, fun, arg, timeout, expr_form, ret, kwarg, **kwargs):
        fcn_ret.update(ret_comp)
        seen += 1
        # fcn_ret can be empty, so we cannot len the whole return dict
        if expr_form == 'list' and len(tgt) == seen:
            # do not wait for timeout when explicit list matching
            # and all results are there
            break
    return fcn_ret


def cmd(tgt,
        fun,
        arg=(),
        timeout=None,
        expr_form='glob',
        ret='',
        kwarg=None,
        ssh=False,
        **kwargs):
    '''
    Assuming this minion is a master, execute a salt command

    CLI Example:

    .. code-block:: bash

        salt '*' saltutil.cmd
    '''
    cfgfile = __opts__['conf_file']
    client = _get_ssh_or_api_client(cfgfile, ssh)
    fcn_ret = _exec(
        client, tgt, fun, arg, timeout, expr_form, ret, kwarg, **kwargs)
    # if return is empty, we may have not used the right conf,
    # try with the 'minion relative master configuration counter part
    # if available
    master_cfgfile = '{0}master'.format(cfgfile[:-6])  # remove 'minion'
    if (
        not fcn_ret
        and cfgfile.endswith('{0}{1}'.format(os.path.sep, 'minion'))
        and os.path.exists(master_cfgfile)
    ):
        client = _get_ssh_or_api_client(master_cfgfile, ssh)
        fcn_ret = _exec(
            client, tgt, fun, arg, timeout, expr_form, ret, kwarg, **kwargs)
    return fcn_ret


def cmd_iter(tgt,
             fun,
             arg=(),
             timeout=None,
             expr_form='glob',
             ret='',
             kwarg=None,
             ssh=False,
             **kwargs):
    '''
    Assuming this minion is a master, execute a salt command

    CLI Example:

    .. code-block:: bash

        salt '*' saltutil.cmd_iter
    '''
    if ssh:
        client = salt.client.ssh.client.SSHClient(__opts__['conf_file'])
    else:
        client = salt.client.get_local_client(__opts__['conf_file'])
    for ret in client.cmd_iter(
            tgt,
            fun,
            arg,
            timeout,
            expr_form,
            ret,
            kwarg,
            **kwargs):
        yield ret


def runner(name, **kwargs):
    '''
    Execute a runner module (this function must be run on the master)

    .. versionadded:: 2014.7.0

    name
        The name of the function to run

    kwargs
        Any keyword arguments to pass to the runner function

    CLI Example:

    .. code-block:: bash

        salt '*' saltutil.runner jobs.list_jobs
    '''
    saltenv = kwargs.pop('__env__', 'base')
    kwargs = salt.utils.clean_kwargs(**kwargs)

    if 'master_job_cache' not in __opts__:
        master_config = os.path.join(os.path.dirname(__opts__['conf_file']),
                                     'master')
        master_opts = salt.config.master_config(master_config)
        rclient = salt.runner.RunnerClient(master_opts)
    else:
        rclient = salt.runner.RunnerClient(__opts__)

    if name in rclient.functions:
        aspec = salt.utils.args.get_function_argspec(rclient.functions[name])
        if 'saltenv' in aspec.args:
            kwargs['saltenv'] = saltenv

    return rclient.cmd(name, kwarg=kwargs, print_event=False)


def wheel(name, *args, **kwargs):
    '''
    Execute a wheel module (this function must be run on the master)

    .. versionadded:: 2014.7.0

    name
        The name of the function to run

    args
        Any positional arguments to pass to the wheel function. A common example
        of this would be the ``match`` arg needed for key functions.

        .. versionadded:: v2015.8.11

    kwargs
        Any keyword arguments to pass to the wheel function

    CLI Example:

    .. code-block:: bash

        salt '*' saltutil.wheel key.accept jerry
    '''
    saltenv = kwargs.pop('__env__', 'base')

    if __opts__['__role'] == 'minion':
        master_config = os.path.join(os.path.dirname(__opts__['conf_file']),
                                     'master')
        master_opts = salt.config.client_config(master_config)
        wheel_client = salt.wheel.WheelClient(master_opts)
    else:
        wheel_client = salt.wheel.WheelClient(__opts__)

    # The WheelClient cmd needs args, kwargs, and pub_data separated out from
    # the "normal" kwargs structure, which at this point contains __pub_x keys.
    pub_data = {}
    valid_kwargs = {}
    for key, val in six.iteritems(kwargs):
        if key.startswith('__'):
            pub_data[key] = val
        else:
            valid_kwargs[key] = val

    try:
        if name in wheel_client.functions:
            aspec = salt.utils.args.get_function_argspec(
                wheel_client.functions[name]
            )
            if 'saltenv' in aspec.args:
                valid_kwargs['saltenv'] = saltenv

        ret = wheel_client.cmd(name,
                               arg=args,
                               pub_data=pub_data,
                               kwarg=valid_kwargs,
                               print_event=False)
    except SaltInvocationError:
        raise CommandExecutionError(
            'This command can only be executed on a minion that is located on '
            'the master.'
        )

    return ret


# this is the only way I could figure out how to get the REAL file_roots
# __opt__['file_roots'] is set to  __opt__['pillar_root']
class _MMinion(object):
    def __new__(cls, saltenv, reload_env=False):
        # this is to break out of salt.loaded.int and make this a true singleton
        # hack until https://github.com/saltstack/salt/pull/10273 is resolved
        # this is starting to look like PHP
        global _mminions  # pylint: disable=W0601
        if '_mminions' not in globals():
            _mminions = {}
        if saltenv not in _mminions or reload_env:
            opts = copy.deepcopy(__opts__)
            del opts['file_roots']
            # grains at this point are in the context of the minion
            global __grains__  # pylint: disable=W0601
            grains = copy.deepcopy(__grains__)
            m = salt.minion.MasterMinion(opts)

            # this assignment is so that the rest of fxns called by salt still
            # have minion context
            __grains__ = grains

            # this assignment is so that fxns called by mminion have minion
            # context
            m.opts['grains'] = grains

            env_roots = m.opts['file_roots'][saltenv]
            m.opts['module_dirs'] = [fp + '/_modules' for fp in env_roots]
            m.gen_modules()
            _mminions[saltenv] = m
        return _mminions[saltenv]


def mmodule(saltenv, fun, *args, **kwargs):
    '''
    Loads minion modules from an environment so that they can be used in pillars
    for that environment

    CLI Example:

    .. code-block:: bash

        salt '*' saltutil.mmodule base test.ping
    '''
    mminion = _MMinion(saltenv)
    return mminion.functions[fun](*args, **kwargs)
