# -*- coding: utf-8 -*-
'''
Routines to set up a minion
'''
# Import python libs
from __future__ import absolute_import, print_function
import os
import re
import sys
import copy
import time
import types
import signal
import fnmatch
import logging
import threading
import traceback
import multiprocessing
from random import randint, shuffle
from salt.config import DEFAULT_MINION_OPTS
from stat import S_IMODE

# Import Salt Libs
# pylint: disable=import-error,no-name-in-module,redefined-builtin
import salt.ext.six as six
if six.PY3:
    import ipaddress
else:
    import salt.ext.ipaddress as ipaddress
from salt.ext.six.moves import range
# pylint: enable=no-name-in-module,redefined-builtin

# Import third party libs
try:
    import zmq
    # TODO: cleanup
    import zmq.eventloop.ioloop
    # support pyzmq 13.0.x, TODO: remove once we force people to 14.0.x
    if not hasattr(zmq.eventloop.ioloop, 'ZMQIOLoop'):
        zmq.eventloop.ioloop.ZMQIOLoop = zmq.eventloop.ioloop.IOLoop
    HAS_ZMQ = True
except ImportError:
    # Running in local, zmq not needed
    HAS_ZMQ = False

HAS_RANGE = False
try:
    import seco.range
    HAS_RANGE = True
except ImportError:
    pass

HAS_PSUTIL = False
try:
    import salt.utils.psutil_compat as psutil
    HAS_PSUTIL = True
except ImportError:
    pass

HAS_RESOURCE = False
try:
    import resource
    HAS_RESOURCE = True
except ImportError:
    pass

try:
    import zmq.utils.monitor
    HAS_ZMQ_MONITOR = True
except ImportError:
    HAS_ZMQ_MONITOR = False
# pylint: enable=import-error

# Import salt libs
import salt
import salt.client
import salt.crypt
import salt.loader
import salt.beacons
import salt.payload
import salt.syspaths
import salt.utils
import salt.utils.jid
import salt.pillar
import salt.utils.args
import salt.utils.event
import salt.utils.minion
import salt.utils.minions
import salt.utils.schedule
import salt.utils.error
import salt.utils.zeromq
import salt.defaults.exitcodes
import salt.cli.daemons

from salt.defaults import DEFAULT_TARGET_DELIM
from salt.utils.debug import enable_sigusr1_handler
from salt.utils.event import tagify
from salt.exceptions import (
    CommandExecutionError,
    CommandNotFoundError,
    SaltInvocationError,
    SaltReqTimeoutError,
    SaltClientError,
    SaltSystemExit,
    SaltException,
)


import tornado.gen  # pylint: disable=F0401
import tornado.ioloop  # pylint: disable=F0401

log = logging.getLogger(__name__)

# To set up a minion:
# 1. Read in the configuration
# 2. Generate the function mapping dict
# 3. Authenticate with the master
# 4. Store the AES key
# 5. Connect to the publisher
# 6. Handle publications


def resolve_dns(opts):
    '''
    Resolves the master_ip and master_uri options
    '''
    ret = {}
    check_dns = True
    if (opts.get('file_client', 'remote') == 'local' and
            not opts.get('use_master_when_local', False)):
        check_dns = False

    if check_dns is True:
        # Because I import salt.log below I need to re-import salt.utils here
        import salt.utils
        try:
            if opts['master'] == '':
                raise SaltSystemExit
            ret['master_ip'] = \
                    salt.utils.dns_check(opts['master'], True, opts['ipv6'])
        except SaltClientError:
            if opts['retry_dns']:
                while True:
                    import salt.log
                    msg = ('Master hostname: \'{0}\' not found. Retrying in {1} '
                           'seconds').format(opts['master'], opts['retry_dns'])
                    if salt.log.is_console_configured():
                        log.error(msg)
                    else:
                        print('WARNING: {0}'.format(msg))
                    time.sleep(opts['retry_dns'])
                    try:
                        ret['master_ip'] = salt.utils.dns_check(
                            opts['master'], True, opts['ipv6']
                        )
                        break
                    except SaltClientError:
                        pass
            else:
                ret['master_ip'] = '127.0.0.1'
        except SaltSystemExit:
            unknown_str = 'unknown address'
            master = opts.get('master', unknown_str)
            if master == '':
                master = unknown_str
            if opts.get('__role') == 'syndic':
                err = 'Master address: \'{0}\' could not be resolved. Invalid or unresolveable address. Set \'syndic_master\' value in minion config.'.format(master)
            else:
                err = 'Master address: \'{0}\' could not be resolved. Invalid or unresolveable address. Set \'master\' value in minion config.'.format(master)
            log.error(err)
            raise SaltSystemExit(code=42, msg=err)
    else:
        ret['master_ip'] = '127.0.0.1'

    if 'master_ip' in ret and 'master_ip' in opts:
        if ret['master_ip'] != opts['master_ip']:
            log.warning('Master ip address changed from {0} to {1}'.format(opts['master_ip'],
                                                                          ret['master_ip'])
            )
    ret['master_uri'] = 'tcp://{ip}:{port}'.format(ip=ret['master_ip'],
                                                   port=opts['master_port'])
    return ret


def prep_ip_port(opts):
    ret = {}
    if opts['master_uri_format'] == 'ip_only':
        ret['master'] = opts['master']
    else:
        ip_port = opts['master'].rsplit(":", 1)
        if len(ip_port) == 1:
            # e.g. master: mysaltmaster
            ret['master'] = ip_port[0]
        else:
            # e.g. master: localhost:1234
            # e.g. master: 127.0.0.1:1234
            # e.g. master: ::1:1234
            ret['master'] = ip_port[0]
            ret['master_port'] = ip_port[1]
    return ret


def get_proc_dir(cachedir, **kwargs):
    '''
    Given the cache directory, return the directory that process data is
    stored in, creating it if it doesn't exist.
    The following optional Keyword Arguments are handled:

    mode: which is anything os.makedir would accept as mode.

    uid: the uid to set, if not set, or it is None or -1 no changes are
         made. Same applies if the directory is already owned by this
         uid. Must be int. Works only on unix/unix like systems.

    gid: the gid to set, if not set, or it is None or -1 no changes are
         made. Same applies if the directory is already owned by this
         gid. Must be int. Works only on unix/unix like systems.
    '''
    fn_ = os.path.join(cachedir, 'proc')
    mode = kwargs.pop('mode', None)

    if mode is None:
        mode = {}
    else:
        mode = {'mode': mode}

    if not os.path.isdir(fn_):
        # proc_dir is not present, create it with mode settings
        os.makedirs(fn_, **mode)

    d_stat = os.stat(fn_)

    # if mode is not an empty dict then we have an explicit
    # dir mode. So lets check if mode needs to be changed.
    if mode:
        mode_part = S_IMODE(d_stat.st_mode)
        if mode_part != mode['mode']:
            os.chmod(fn_, (d_stat.st_mode ^ mode_part) | mode['mode'])

    if hasattr(os, 'chown'):
        # only on unix/unix like systems
        uid = kwargs.pop('uid', -1)
        gid = kwargs.pop('gid', -1)

        # if uid and gid are both -1 then go ahead with
        # no changes at all
        if (d_stat.st_uid != uid or d_stat.st_gid != gid) and \
                [i for i in (uid, gid) if i != -1]:
            os.chown(fn_, uid, gid)

    return fn_


def parse_args_and_kwargs(func, args, data=None):
    '''
    Wrap load_args_and_kwargs
    '''
    salt.utils.warn_until(
        'Boron',
        'salt.minion.parse_args_and_kwargs() has been renamed to '
        'salt.minion.load_args_and_kwargs(). Please change this function call '
        'before the Boron release of Salt.'
    )
    return load_args_and_kwargs(func, args, data=data)


def load_args_and_kwargs(func, args, data=None, ignore_invalid=False):
    '''
    Detect the args and kwargs that need to be passed to a function call, and
    check them against what was passed.
    '''
    argspec = salt.utils.args.get_function_argspec(func)
    _args = []
    _kwargs = {}
    invalid_kwargs = []

    for arg in args:
        if isinstance(arg, six.string_types):
            string_arg, string_kwarg = salt.utils.args.parse_input([arg], condition=False)  # pylint: disable=W0632
            if string_arg:
                # Don't append the version that was just derived from parse_cli
                # above, that would result in a 2nd call to
                # salt.utils.cli.yamlify_arg(), which could mangle the input.
                _args.append(arg)
            elif string_kwarg:
                salt.utils.warn_until(
                    'Boron',
                    'The list of function args and kwargs should be parsed '
                    'by salt.utils.args.parse_input() before calling '
                    'salt.minion.load_args_and_kwargs().'
                )
                if argspec.keywords or next(six.iterkeys(string_kwarg)) in argspec.args:
                    # Function supports **kwargs or is a positional argument to
                    # the function.
                    _kwargs.update(string_kwarg)
                else:
                    # **kwargs not in argspec and parsed argument name not in
                    # list of positional arguments. This keyword argument is
                    # invalid.
                    for key, val in six.iteritems(string_kwarg):
                        invalid_kwargs.append('{0}={1}'.format(key, val))
                continue

        # if the arg is a dict with __kwarg__ == True, then its a kwarg
        elif isinstance(arg, dict) and arg.pop('__kwarg__', False) is True:
            for key, val in six.iteritems(arg):
                if argspec.keywords or key in argspec.args:
                    # Function supports **kwargs or is a positional argument to
                    # the function.
                    _kwargs[key] = val
                else:
                    # **kwargs not in argspec and parsed argument name not in
                    # list of positional arguments. This keyword argument is
                    # invalid.
                    invalid_kwargs.append('{0}={1}'.format(key, val))
            continue

        else:
            _args.append(arg)

    if invalid_kwargs and not ignore_invalid:
        salt.utils.invalid_kwargs(invalid_kwargs)

    if argspec.keywords and isinstance(data, dict):
        # this function accepts **kwargs, pack in the publish data
        for key, val in six.iteritems(data):
            _kwargs['__pub_{0}'.format(key)] = val

    return _args, _kwargs


class MinionBase(object):
    def __init__(self, opts):
        self.opts = opts

    @staticmethod
    def process_schedule(minion, loop_interval):
        try:
            if hasattr(minion, 'schedule'):
                minion.schedule.eval()
            else:
                log.error('Minion scheduler not initialized. Scheduled jobs will not be run.')
                return
            # Check if scheduler requires lower loop interval than
            # the loop_interval setting
            if minion.schedule.loop_interval < loop_interval:
                loop_interval = minion.schedule.loop_interval
                log.debug(
                    'Overriding loop_interval because of scheduled jobs.'
                )
        except Exception as exc:
            log.error(
                'Exception {0} occurred in scheduled job'.format(exc)
            )
        return loop_interval

    def process_beacons(self, functions):
        '''
        Evaluate all of the configured beacons, grab the config again in case
        the pillar or grains changed
        '''
        if 'config.merge' in functions:
            b_conf = functions['config.merge']('beacons')
            if b_conf:
                return self.beacons.process(b_conf)
        return []

    @tornado.gen.coroutine
    def eval_master(self,
                    opts,
                    timeout=60,
                    safe=True,
                    failed=False):
        '''
        Evaluates and returns a tuple of the current master address and the pub_channel.

        In standard mode, just creates a pub_channel with the given master address.

        With master_type=func evaluates the current master address from the given
        module and then creates a pub_channel.

        With master_type=failover takes the list of masters and loops through them.
        The first one that allows the minion to create a pub_channel is then
        returned. If this function is called outside the minions initialization
        phase (for example from the minions main event-loop when a master connection
        loss was detected), 'failed' should be set to True. The current
        (possibly failed) master will then be removed from the list of masters.
        '''
        # check if master_type was altered from its default
        if opts['master_type'] != 'str' and opts['__role'] != 'syndic':
            # check for a valid keyword
            if opts['master_type'] == 'func':
                # split module and function and try loading the module
                mod, fun = opts['master'].split('.')
                try:
                    master_mod = salt.loader.raw_mod(opts, mod, fun)
                    if not master_mod:
                        raise TypeError
                    # we take whatever the module returns as master address
                    opts['master'] = master_mod[mod + '.' + fun]()
                except TypeError:
                    msg = ('Failed to evaluate master address from '
                           'module \'{0}\''.format(opts['master']))
                    log.error(msg)
                    sys.exit(salt.defaults.exitcodes.EX_GENERIC)
                log.info('Evaluated master from module: {0}'.format(master_mod))

            # if failover is set, master has to be of type list
            elif opts['master_type'] == 'failover':
                if isinstance(opts['master'], list):
                    log.info('Got list of available master addresses:'
                             ' {0}'.format(opts['master']))
                    if opts['master_shuffle']:
                        shuffle(opts['master'])
                    opts['auth_tries'] = 0
                # if opts['master'] is a str and we have never created opts['master_list']
                elif isinstance(opts['master'], str) and ('master_list' not in opts):
                    # We have a string, but a list was what was intended. Convert.
                    # See issue 23611 for details
                    opts['master'] = [opts['master']]
                elif opts['__role'] == 'syndic':
                    log.info('Syndic setting master_syndic to \'{0}\''.format(opts['master']))

                # if failed=True, the minion was previously connected
                # we're probably called from the minions main-event-loop
                # because a master connection loss was detected. remove
                # the possibly failed master from the list of masters.
                elif failed:
                    log.info('Removing possibly failed master {0} from list of'
                             ' masters'.format(opts['master']))
                    # create new list of master with the possibly failed one removed
                    opts['master'] = [x for x in opts['master_list'] if opts['master'] != x]

                else:
                    msg = ('master_type set to \'failover\' but \'master\' '
                           'is not of type list but of type '
                           '{0}'.format(type(opts['master'])))
                    log.error(msg)
                    sys.exit(salt.defaults.exitcodes.EX_GENERIC)
                # If failover is set, minion have to failover on DNS errors instead of retry DNS resolve.
                # See issue 21082 for details
                if opts['retry_dns']:
                    msg = ('\'master_type\' set to \'failover\' but \'retry_dns\' is not 0. '
                           'Setting \'retry_dns\' to 0 to failover to the next master on DNS errors.')
                    log.critical(msg)
                    opts['retry_dns'] = 0
            else:
                msg = ('Invalid keyword \'{0}\' for variable '
                       '\'master_type\''.format(opts['master_type']))
                log.error(msg)
                sys.exit(salt.defaults.exitcodes.EX_GENERIC)

        # FIXME: if SMinion don't define io_loop, it can't switch master see #29088
        # Specify kwargs for the channel factory so that SMinion doesn't need to define an io_loop
        # (The channel factories will set a default if the kwarg isn't passed)
        factory_kwargs = {'timeout': timeout, 'safe': safe}
        if getattr(self, 'io_loop', None):
            factory_kwargs['io_loop'] = self.io_loop

        # if we have a list of masters, loop through them and be
        # happy with the first one that allows us to connect
        if isinstance(opts['master'], list):
            conn = False
            # shuffle the masters and then loop through them
            local_masters = copy.copy(opts['master'])

            for master in local_masters:
                opts['master'] = master
                opts.update(prep_ip_port(opts))
                opts.update(resolve_dns(opts))
                self.opts = opts

                # on first run, update self.opts with the whole master list
                # to enable a minion to re-use old masters if they get fixed
                if 'master_list' not in opts:
                    opts['master_list'] = local_masters

                try:
                    pub_channel = salt.transport.client.AsyncPubChannel.factory(opts, **factory_kwargs)
                    yield pub_channel.connect()
                    conn = True
                    break
                except SaltClientError:
                    msg = ('Master {0} could not be reached, trying '
                           'next master (if any)'.format(opts['master']))
                    log.info(msg)
                    continue

            if not conn:
                self.connected = False
                msg = ('No master could be reached or all masters denied '
                       'the minions connection attempt.')
                log.error(msg)
                raise SaltClientError(msg)
            else:
                self.tok = pub_channel.auth.gen_token('salt')
                self.connected = True
                raise tornado.gen.Return((opts['master'], pub_channel))

        # single master sign in
        else:
            opts.update(prep_ip_port(opts))
            opts.update(resolve_dns(opts))
            pub_channel = salt.transport.client.AsyncPubChannel.factory(self.opts, **factory_kwargs)
            yield pub_channel.connect()
            self.tok = pub_channel.auth.gen_token('salt')
            self.connected = True
            raise tornado.gen.Return((opts['master'], pub_channel))


class SMinion(MinionBase):
    '''
    Create an object that has loaded all of the minion module functions,
    grains, modules, returners etc.  The SMinion allows developers to
    generate all of the salt minion functions and present them with these
    functions for general use.
    '''
    def __init__(self, opts):
        # Late setup of the opts grains, so we can log from the grains module
        opts['grains'] = salt.loader.grains(opts)
        super(SMinion, self).__init__(opts)

        # Clean out the proc directory (default /var/cache/salt/minion/proc)
        if (self.opts.get('file_client', 'remote') == 'remote' or
                self.opts.get('use_master_when_local', False)):
            # actually eval_master returns the future and we need to wait for it
            self.io_loop = zmq.eventloop.ioloop.ZMQIOLoop()
            self.io_loop.run_sync(lambda: self.eval_master(self.opts, failed=True))
        self.gen_modules(initial_load=True)

    def gen_modules(self, initial_load=False):
        '''
        Load all of the modules for the minion
        '''
        self.opts['pillar'] = salt.pillar.get_pillar(
            self.opts,
            self.opts['grains'],
            self.opts['id'],
            self.opts['environment'],
            pillarenv=self.opts.get('pillarenv'),
        ).compile_pillar()
        self.utils = salt.loader.utils(self.opts)
        self.functions = salt.loader.minion_mods(self.opts, utils=self.utils,
                                                 include_errors=True)
        self.returners = salt.loader.returners(self.opts, self.functions)
        self.proxy = salt.loader.proxy(self.opts, self.functions, self.returners, None)
        # TODO: remove
        self.function_errors = {}  # Keep the funcs clean
        self.states = salt.loader.states(self.opts, self.functions, self.utils)
        self.rend = salt.loader.render(self.opts, self.functions)
        self.matcher = Matcher(self.opts, self.functions)
        self.functions['sys.reload_modules'] = self.gen_modules


class MasterMinion(object):
    '''
    Create a fully loaded minion function object for generic use on the
    master. What makes this class different is that the pillar is
    omitted, otherwise everything else is loaded cleanly.
    '''
    def __init__(
            self,
            opts,
            returners=True,
            states=True,
            rend=True,
            matcher=True,
            whitelist=None,
            ignore_config_errors=True):
        self.opts = salt.config.minion_config(opts['conf_file'], ignore_config_errors=ignore_config_errors)
        self.opts.update(opts)
        self.whitelist = whitelist
        self.opts['grains'] = salt.loader.grains(opts)
        self.opts['pillar'] = {}
        self.mk_returners = returners
        self.mk_states = states
        self.mk_rend = rend
        self.mk_matcher = matcher
        self.gen_modules(initial_load=True)

    def gen_modules(self, initial_load=False):
        '''
        Load all of the modules for the minion
        '''
        self.utils = salt.loader.utils(self.opts)
        self.functions = salt.loader.minion_mods(
            self.opts,
            utils=self.utils,
            whitelist=self.whitelist,
            initial_load=initial_load)
        if self.mk_returners:
            self.returners = salt.loader.returners(self.opts, self.functions)
        if self.mk_states:
            self.states = salt.loader.states(self.opts,
                                             self.functions,
                                             self.utils)
        if self.mk_rend:
            self.rend = salt.loader.render(self.opts, self.functions)
        if self.mk_matcher:
            self.matcher = Matcher(self.opts, self.functions)
        self.functions['sys.reload_modules'] = self.gen_modules


class MultiMinion(MinionBase):
    '''
    Create a multi minion interface, this creates as many minions as are
    defined in the master option and binds each minion object to a respective
    master.
    '''
    # timeout for one of the minions to auth with a master
    MINION_CONNECT_TIMEOUT = 5

    def __init__(self, opts):
        super(MultiMinion, self).__init__(opts)
        self.auth_wait = self.opts['acceptance_wait_time']
        self.max_auth_wait = self.opts['acceptance_wait_time_max']

        self.io_loop = zmq.eventloop.ioloop.ZMQIOLoop()

    def _spawn_minions(self):
        '''
        Spawn all the coroutines which will sign in to masters
        '''
        if not isinstance(self.opts['master'], list):
            log.error(
                'Attempting to start a multimaster system with one master')
            sys.exit(salt.defaults.exitcodes.EX_GENERIC)
        # Check that for tcp ipc_mode that we have either default ports or
        # lists of ports
        if self.opts.get('ipc_mode') == 'tcp' and (
                    (not isinstance(self.opts['tcp_pub_port'], list) and
                    self.opts['tcp_pub_port'] != 4510) or
                    (not isinstance(self.opts['tcp_pull_port'], list) and
                    self.opts['tcp_pull_port'] != 4511)
                ):
            raise SaltException('For multi-master, tcp_(pub/pull)_port '
                                'settings must be lists of ports, or the '
                                'default 4510 and 4511')
        masternumber = 0
        for master in set(self.opts['master']):
            s_opts = copy.deepcopy(self.opts)
            s_opts['master'] = master
            s_opts['multimaster'] = True
            s_opts['auth_timeout'] = self.MINION_CONNECT_TIMEOUT
            if self.opts.get('ipc_mode') == 'tcp':
                # If one is a list, we can assume both are, because of check above
                if isinstance(self.opts['tcp_pub_port'], list):
                    s_opts['tcp_pub_port'] = self.opts['tcp_pub_port'][masternumber]
                    s_opts['tcp_pull_port'] = self.opts['tcp_pull_port'][masternumber]
                else:
                    s_opts['tcp_pub_port'] = self.opts['tcp_pub_port'] + (masternumber * 2)
                    s_opts['tcp_pull_port'] = self.opts['tcp_pull_port'] + (masternumber * 2)
            self.io_loop.spawn_callback(self._connect_minion, s_opts)
            masternumber += 1

    @tornado.gen.coroutine
    def _connect_minion(self, opts):
        '''
        Create a minion, and asynchronously connect it to a master
        '''
        last = 0  # never have we signed in
        auth_wait = opts['acceptance_wait_time']
        while True:
            try:
                minion = Minion(opts,
                                self.MINION_CONNECT_TIMEOUT,
                                False,
                                io_loop=self.io_loop,
                                loaded_base_name='salt.loader.{0}'.format(opts['master']),
                                )
                yield minion.connect_master()
                minion.tune_in(start=False)
                break
            except SaltClientError as exc:
                log.error('Error while bringing up minion for multi-master. Is master at {0} responding?'.format(opts['master']))
                last = time.time()
                if auth_wait < self.max_auth_wait:
                    auth_wait += self.auth_wait
                yield tornado.gen.sleep(auth_wait)  # TODO: log?
            except Exception as e:
                log.critical('Unexpected error while connecting to {0}'.format(opts['master']), exc_info=True)

    # Multi Master Tune In
    def tune_in(self):
        '''
        Bind to the masters

        This loop will attempt to create connections to masters it hasn't connected
        to yet, but once the initial connection is made it is up to ZMQ to do the
        reconnect (don't know of an API to get the state here in salt)
        '''
        # Fire off all the minion coroutines
        self.minions = self._spawn_minions()

        # serve forever!
        self.io_loop.start()


class Minion(MinionBase):
    '''
    This class instantiates a minion, runs connections for a minion,
    and loads all of the functions into the minion
    '''
    def __init__(self, opts, timeout=60, safe=True, loaded_base_name=None, io_loop=None):  # pylint: disable=W0231
        '''
        Pass in the options dict
        '''
        # this means that the parent class doesn't know *which* master we connect to
        super(Minion, self).__init__(opts)
        self.timeout = timeout
        self.safe = safe

        self._running = None
        self.win_proc = []
        self.loaded_base_name = loaded_base_name
        self.restart = False

        self.io_loop = io_loop or zmq.eventloop.ioloop.ZMQIOLoop()
        if not self.io_loop.initialized():
            self.io_loop.install()

        # Warn if ZMQ < 3.2
        if HAS_ZMQ:
            try:
                zmq_version_info = zmq.zmq_version_info()
            except AttributeError:
                # PyZMQ <= 2.1.9 does not have zmq_version_info, fall back to
                # using zmq.zmq_version() and build a version info tuple.
                zmq_version_info = tuple(
                    [int(x) for x in zmq.zmq_version().split('.')]
                )
            if zmq_version_info < (3, 2):
                log.warning(
                    'You have a version of ZMQ less than ZMQ 3.2! There are '
                    'known connection keep-alive issues with ZMQ < 3.2 which '
                    'may result in loss of contact with minions. Please '
                    'upgrade your ZMQ!'
                )
        # Late setup the of the opts grains, so we can log from the grains
        # module.  If this is a proxy, however, we need to init the proxymodule
        # before we can get the grains.  We do this for proxies in the
        # post_master_init
        if not salt.utils.is_proxy():
            self.opts['grains'] = salt.loader.grains(opts)

    # TODO: remove?
    def sync_connect_master(self):
        '''
        Block until we are connected to a master
        '''
        self._connect_master_future = self.connect_master()
        # finish connecting to master
        self._connect_master_future.add_done_callback(lambda f: self.io_loop.stop())
        try:
            self.io_loop.start()
        except KeyboardInterrupt:
            self.destroy()
        # I made the following 3 line oddity to preserve traceback.
        # Please read PR #23978 before changing, hopefully avoiding regressions.
        # Good luck, we're all counting on you.  Thanks.
        future_exception = self._connect_master_future.exc_info()
        if future_exception:
            # This needs to be re-raised to preserve restart_on_error behavior.
            raise six.reraise(*future_exception)

    @tornado.gen.coroutine
    def connect_master(self):
        '''
        Return a future which will complete when you are connected to a master
        '''
        master, self.pub_channel = yield self.eval_master(self.opts, self.timeout, self.safe)
        yield self._post_master_init(master)

    # TODO: better name...
    @tornado.gen.coroutine
    def _post_master_init(self, master):
        '''
        Function to finish init after connecting to a master

        This is primarily loading modules, pillars, etc. (since they need
        to know which master they connected to)
        '''
        self.opts['master'] = master

        self.opts['pillar'] = yield salt.pillar.get_async_pillar(
            self.opts,
            self.opts['grains'],
            self.opts['id'],
            self.opts['environment'],
            pillarenv=self.opts.get('pillarenv'),
        ).compile_pillar()
        self.functions, self.returners, self.function_errors = self._load_modules()
        self.serial = salt.payload.Serial(self.opts)
        self.mod_opts = self._prep_mod_opts()
        self.matcher = Matcher(self.opts, self.functions)
        self.beacons = salt.beacons.Beacon(self.opts, self.functions)
        uid = salt.utils.get_uid(user=self.opts.get('user', None))
        self.proc_dir = get_proc_dir(self.opts['cachedir'], uid=uid)

        self.schedule = salt.utils.schedule.Schedule(
            self.opts,
            self.functions,
            self.returners)

        # add default scheduling jobs to the minions scheduler
        if self.opts['mine_enabled'] and 'mine.update' in self.functions:
            self.schedule.add_job({
                '__mine_interval':
                {
                    'function': 'mine.update',
                    'minutes': self.opts['mine_interval'],
                    'jid_include': True,
                    'maxrunning': 2,
                    'return_job': self.opts.get('mine_return_job', False)
                }
            }, persist=True)

        # add master_alive job if enabled
        if self.opts['master_alive_interval'] > 0:
            self.schedule.add_job({
                '__master_alive':
                {
                    'function': 'status.master',
                    'seconds': self.opts['master_alive_interval'],
                    'jid_include': True,
                    'maxrunning': 1,
                    'kwargs': {'master': self.opts['master'],
                               'connected': True}
                }
            }, persist=True)

        self.grains_cache = self.opts['grains']

    def _return_retry_timer(self):
        '''
        Based on the minion configuration, either return a randomized timer or
        just return the value of the return_retry_timer.
        '''
        msg = 'Minion return retry timer set to {0} seconds'
        if self.opts.get('return_retry_timer_max'):
            try:
                random_retry = randint(self.opts['return_retry_timer'], self.opts['return_retry_timer_max'])
                log.debug(msg.format(random_retry) + ' (randomized)')
                return random_retry
            except ValueError:
                # Catch wiseguys using negative integers here
                log.error(
                    'Invalid value (return_retry_timer: {0} or return_retry_timer_max: {1})'
                    'both must be a positive integers'.format(
                        self.opts['return_retry_timer'],
                        self.opts['return_retry_timer_max'],
                    )
                )
                log.debug(msg.format(DEFAULT_MINION_OPTS['return_retry_timer']))
                return DEFAULT_MINION_OPTS['return_retry_timer']
        else:
            log.debug(msg.format(self.opts.get('return_retry_timer')))
            return self.opts.get('return_retry_timer')

    def _prep_mod_opts(self):
        '''
        Returns a copy of the opts with key bits stripped out
        '''
        mod_opts = {}
        for key, val in six.iteritems(self.opts):
            if key == 'logger':
                continue
            mod_opts[key] = val
        return mod_opts

    def _process_beacons(self):
        '''
        Process each beacon and send events if appropriate
        '''
        # Process Beacons
        try:
            beacons = self.process_beacons(self.functions)
        except Exception as exc:
            log.critical('Beacon processing failed: {0}. No beacons will be processed.'.format(traceback.format_exc(exc)))
            beacons = None
        if beacons:
            self._fire_master(events=beacons)
            for beacon in beacons:
                serialized_data = salt.utils.dicttrim.trim_dict(
                    self.serial.dumps(beacon['data']),
                    self.opts.get('max_event_size', 1048576),
                    is_msgpacked=True,
                )
                log.debug('Sending event - data = {0}'.format(beacon['data']))
                event = '{0}{1}{2}'.format(
                        beacon['tag'],
                        salt.utils.event.TAGEND,
                        serialized_data,
                )
                self.event_publisher.handle_publish([event])

    def _load_modules(self, force_refresh=False, notify=False, proxy=None):
        '''
        Return the functions and the returners loaded up from the loader
        module
        '''
        # if this is a *nix system AND modules_max_memory is set, lets enforce
        # a memory limit on module imports
        # this feature ONLY works on *nix like OSs (resource module doesn't work on windows)
        modules_max_memory = False
        if self.opts.get('modules_max_memory', -1) > 0 and HAS_PSUTIL and HAS_RESOURCE:
            log.debug('modules_max_memory set, enforcing a maximum of {0}'.format(self.opts['modules_max_memory']))
            modules_max_memory = True
            old_mem_limit = resource.getrlimit(resource.RLIMIT_AS)
            rss, vms = psutil.Process(os.getpid()).memory_info()
            mem_limit = rss + vms + self.opts['modules_max_memory']
            resource.setrlimit(resource.RLIMIT_AS, (mem_limit, mem_limit))
        elif self.opts.get('modules_max_memory', -1) > 0:
            if not HAS_PSUTIL:
                log.error('Unable to enforce modules_max_memory because psutil is missing')
            if not HAS_RESOURCE:
                log.error('Unable to enforce modules_max_memory because resource is missing')

        self.opts['grains'] = salt.loader.grains(self.opts, force_refresh)
        self.utils = salt.loader.utils(self.opts)
        if self.opts.get('multimaster', False):
            s_opts = copy.deepcopy(self.opts)
            functions = salt.loader.minion_mods(s_opts, utils=self.utils, proxy=proxy,
                                                loaded_base_name=self.loaded_base_name, notify=notify)
        else:
            functions = salt.loader.minion_mods(self.opts, utils=self.utils, notify=notify, proxy=proxy)
        returners = salt.loader.returners(self.opts, functions)
        errors = {}
        if '_errors' in functions:
            errors = functions['_errors']
            functions.pop('_errors')

        # we're done, reset the limits!
        if modules_max_memory is True:
            resource.setrlimit(resource.RLIMIT_AS, old_mem_limit)

        return functions, returners, errors

    def _fire_master(self, data=None, tag=None, events=None, pretag=None, timeout=60):
        '''
        Fire an event on the master, or drop message if unable to send.
        '''
        load = {'id': self.opts['id'],
                'cmd': '_minion_event',
                'pretag': pretag,
                'tok': self.tok}
        if events:
            load['events'] = events
        elif data and tag:
            load['data'] = data
            load['tag'] = tag
        elif not data and tag:
            load['data'] = {}
            load['tag'] = tag
        else:
            return
        channel = salt.transport.Channel.factory(self.opts)
        try:
            result = channel.send(load, timeout=timeout)
            return True
        except salt.exceptions.SaltReqTimeoutError:
            log.info('fire_master failed: master could not be contacted. Request timed out.')
            return False
        except Exception:
            log.info('fire_master failed: {0}'.format(traceback.format_exc()))
            return False

    def _handle_decoded_payload(self, data):
        '''
        Override this method if you wish to handle the decoded data
        differently.
        '''
        if 'user' in data:
            log.info(
                'User {0[user]} Executing command {0[fun]} with jid '
                '{0[jid]}'.format(data)
            )
        else:
            log.info(
                'Executing command {0[fun]} with jid {0[jid]}'.format(data)
            )
        log.debug('Command details {0}'.format(data))

        if isinstance(data['fun'], six.string_types):
            if data['fun'] == 'sys.reload_modules':
                self.functions, self.returners, self.function_errors = self._load_modules()
                self.schedule.functions = self.functions
                self.schedule.returners = self.returners
        if isinstance(data['fun'], tuple) or isinstance(data['fun'], list):
            target = Minion._thread_multi_return
        else:
            target = Minion._thread_return
        # We stash an instance references to allow for the socket
        # communication in Windows. You can't pickle functions, and thus
        # python needs to be able to reconstruct the reference on the other
        # side.
        instance = self
        if self.opts['multiprocessing']:
            if sys.platform.startswith('win'):
                # let python reconstruct the minion on the other side if we're
                # running on windows
                instance = None
            process = multiprocessing.Process(
                target=target, args=(instance, self.opts, data)
            )
        else:
            process = threading.Thread(
                target=target,
                args=(instance, self.opts, data),
                name=data['jid']
            )
        process.start()
        if not sys.platform.startswith('win'):
            process.join()
        else:
            self.win_proc.append(process)

    @classmethod
    def _thread_return(cls, minion_instance, opts, data):
        '''
        This method should be used as a threading target, start the actual
        minion side execution.
        '''
        # this seems awkward at first, but it's a workaround for Windows
        # multiprocessing communication.
        if sys.platform.startswith('win') and \
                opts['multiprocessing'] and \
                not salt.log.is_logging_configured():
            # We have to re-init the logging system for Windows
            salt.log.setup_console_logger(log_level=opts.get('log_level', 'info'))
            if opts.get('log_file'):
                salt.log.setup_logfile_logger(opts['log_file'], opts.get('log_level_logfile', 'info'))
        if not minion_instance:
            minion_instance = cls(opts)
            if not hasattr(minion_instance, 'functions'):
                functions, returners, function_errors = (
                    minion_instance._load_modules()
                    )
                minion_instance.functions = functions
                minion_instance.returners = returners
                minion_instance.function_errors = function_errors
            if not hasattr(minion_instance, 'serial'):
                minion_instance.serial = salt.payload.Serial(opts)
            if not hasattr(minion_instance, 'proc_dir'):
                uid = salt.utils.get_uid(user=opts.get('user', None))
                minion_instance.proc_dir = (
                    get_proc_dir(opts['cachedir'], uid=uid)
                    )

        fn_ = os.path.join(minion_instance.proc_dir, data['jid'])
        if opts['multiprocessing']:
            salt.utils.daemonize_if(opts)

        salt.utils.appendproctitle(data['jid'])

        sdata = {'pid': os.getpid()}
        sdata.update(data)
        log.info('Starting a new job with PID {0}'.format(sdata['pid']))
        with salt.utils.fopen(fn_, 'w+b') as fp_:
            fp_.write(minion_instance.serial.dumps(sdata))
        ret = {'success': False}
        function_name = data['fun']
        if function_name in minion_instance.functions:
            try:
                func = minion_instance.functions[data['fun']]
                args, kwargs = load_args_and_kwargs(
                    func,
                    data['arg'],
                    data)
                minion_instance.functions.pack['__context__']['retcode'] = 0
                if opts.get('sudo_user', ''):
                    sudo_runas = opts.get('sudo_user')
                    if 'sudo.salt_call' in minion_instance.functions:
                        return_data = minion_instance.functions['sudo.salt_call'](
                                sudo_runas,
                                data['fun'],
                                *args,
                                **kwargs)
                else:
                    return_data = func(*args, **kwargs)
                if isinstance(return_data, types.GeneratorType):
                    ind = 0
                    iret = {}
                    for single in return_data:
                        if isinstance(single, dict) and isinstance(iret, dict):
                            iret.update(single)
                        else:
                            if not iret:
                                iret = []
                            iret.append(single)
                        tag = tagify([data['jid'], 'prog', opts['id'], str(ind)], 'job')
                        event_data = {'return': single}
                        minion_instance._fire_master(event_data, tag)
                        ind += 1
                    ret['return'] = iret
                else:
                    ret['return'] = return_data
                ret['retcode'] = minion_instance.functions.pack['__context__'].get(
                    'retcode',
                    0
                )
                ret['success'] = True
            except CommandNotFoundError as exc:
                msg = 'Command required for {0!r} not found'.format(
                    function_name
                )
                log.debug(msg, exc_info=True)
                ret['return'] = '{0}: {1}'.format(msg, exc)
                ret['out'] = 'nested'
            except CommandExecutionError as exc:
                log.error(
                    'A command in {0!r} had a problem: {1}'.format(
                        function_name,
                        exc
                    ),
                    exc_info_on_loglevel=logging.DEBUG
                )
                ret['return'] = 'ERROR: {0}'.format(exc)
                ret['out'] = 'nested'
            except SaltInvocationError as exc:
                log.error(
                    'Problem executing {0!r}: {1}'.format(
                        function_name,
                        exc
                    ),
                    exc_info_on_loglevel=logging.DEBUG
                )
                ret['return'] = 'ERROR executing {0!r}: {1}'.format(
                    function_name, exc
                )
                ret['out'] = 'nested'
            except TypeError as exc:
                msg = 'Passed invalid arguments to {0}: {1}\n{2}'.format(function_name, exc, func.__doc__, )
                log.warning(msg, exc_info_on_loglevel=logging.DEBUG)
                ret['return'] = msg
                ret['out'] = 'nested'
            except Exception:
                msg = 'The minion function caused an exception'
                log.warning(msg, exc_info_on_loglevel=logging.DEBUG)
                salt.utils.error.fire_exception(salt.exceptions.MinionError(msg), opts, job=data)
                ret['return'] = '{0}: {1}'.format(msg, traceback.format_exc())
                ret['out'] = 'nested'
        else:
            ret['return'] = minion_instance.functions.missing_fun_string(function_name)
            mod_name = function_name.split('.')[0]
            if mod_name in minion_instance.function_errors:
                ret['return'] += ' Possible reasons: {0!r}'.format(minion_instance.function_errors[mod_name])
            ret['success'] = False
            ret['retcode'] = 254
            ret['out'] = 'nested'

        ret['jid'] = data['jid']
        ret['fun'] = data['fun']
        ret['fun_args'] = data['arg']
        if 'master_id' in data:
            ret['master_id'] = data['master_id']
        if 'metadata' in data:
            if isinstance(data['metadata'], dict):
                ret['metadata'] = data['metadata']
            else:
                log.warning('The metadata parameter must be a dictionary.  Ignoring.')
        minion_instance._return_pub(
            ret,
            timeout=minion_instance._return_retry_timer()
        )
        if data['ret']:
            if 'ret_config' in data:
                ret['ret_config'] = data['ret_config']
            ret['id'] = opts['id']
            for returner in set(data['ret'].split(',')):
                try:
                    minion_instance.returners['{0}.returner'.format(
                        returner
                    )](ret)
                except Exception as exc:
                    log.error(
                        'The return failed for job {0} {1}'.format(
                        data['jid'],
                        exc
                        )
                    )
                    log.error(traceback.format_exc())

    @classmethod
    def _thread_multi_return(cls, minion_instance, opts, data):
        '''
        This method should be used as a threading target, start the actual
        minion side execution.
        '''
        salt.utils.appendproctitle(data['jid'])
        # this seems awkward at first, but it's a workaround for Windows
        # multiprocessing communication.
        if sys.platform.startswith('win') and \
                opts['multiprocessing'] and \
                not salt.log.is_logging_configured():
            # We have to re-init the logging system for Windows
            salt.log.setup_console_logger(log_level=opts.get('log_level', 'info'))
            if opts.get('log_file'):
                salt.log.setup_logfile_logger(opts['log_file'], opts.get('log_level_logfile', 'info'))
        if not minion_instance:
            minion_instance = cls(opts)
        ret = {
            'return': {},
            'success': {},
        }
        for ind in range(0, len(data['fun'])):
            ret['success'][data['fun'][ind]] = False
            try:
                func = minion_instance.functions[data['fun'][ind]]
                args, kwargs = load_args_and_kwargs(
                    func,
                    data['arg'][ind],
                    data)
                ret['return'][data['fun'][ind]] = func(*args, **kwargs)
                ret['success'][data['fun'][ind]] = True
            except Exception as exc:
                trb = traceback.format_exc()
                log.warning(
                    'The minion function caused an exception: {0}'.format(
                        exc
                    )
                )
                ret['return'][data['fun'][ind]] = trb
            ret['jid'] = data['jid']
            ret['fun'] = data['fun']
            ret['fun_args'] = data['arg']
        if 'metadata' in data:
            ret['metadata'] = data['metadata']
        minion_instance._return_pub(
            ret,
            timeout=minion_instance._return_retry_timer()
        )
        if data['ret']:
            if 'ret_config' in data:
                ret['ret_config'] = data['ret_config']
            for returner in set(data['ret'].split(',')):
                ret['id'] = opts['id']
                try:
                    minion_instance.returners['{0}.returner'.format(
                        returner
                    )](ret)
                except Exception as exc:
                    log.error(
                        'The return failed for job {0} {1}'.format(
                        data['jid'],
                        exc
                        )
                    )

    def _return_pub(self, ret, ret_cmd='_return', timeout=60):
        '''
        Return the data from the executed command to the master server
        '''
        jid = ret.get('jid', ret.get('__jid__'))
        fun = ret.get('fun', ret.get('__fun__'))
        if self.opts['multiprocessing']:
            fn_ = os.path.join(self.proc_dir, jid)
            if os.path.isfile(fn_):
                try:
                    os.remove(fn_)
                except (OSError, IOError):
                    # The file is gone already
                    pass
        log.info('Returning information for job: {0}'.format(jid))
        channel = salt.transport.Channel.factory(self.opts)
        if ret_cmd == '_syndic_return':
            load = {'cmd': ret_cmd,
                    'id': self.opts['id'],
                    'jid': jid,
                    'fun': fun,
                    'arg': ret.get('arg'),
                    'tgt': ret.get('tgt'),
                    'tgt_type': ret.get('tgt_type'),
                    'load': ret.get('__load__')}
            if '__master_id__' in ret:
                load['master_id'] = ret['__master_id__']
            load['return'] = {}
            for key, value in six.iteritems(ret):
                if key.startswith('__'):
                    continue
                load['return'][key] = value
        else:
            load = {'cmd': ret_cmd,
                    'id': self.opts['id']}
            for key, value in six.iteritems(ret):
                load[key] = value

        if 'out' in ret:
            if isinstance(ret['out'], six.string_types):
                load['out'] = ret['out']
            else:
                log.error('Invalid outputter {0}. This is likely a bug.'
                          .format(ret['out']))
        else:
            try:
                oput = self.functions[fun].__outputter__
            except (KeyError, AttributeError, TypeError):
                pass
            else:
                if isinstance(oput, six.string_types):
                    load['out'] = oput
        if self.opts['cache_jobs']:
            # Local job cache has been enabled
            salt.utils.minion.cache_jobs(self.opts, load['jid'], ret)

        try:
            ret_val = channel.send(load, timeout=timeout)
        except SaltReqTimeoutError:
            msg = ('The minion failed to return the job information for job '
                   '{0}. This is often due to the master being shut down or '
                   'overloaded. If the master is running consider increasing '
                   'the worker_threads value.').format(jid)
            log.warn(msg)
            return ''

        log.trace('ret_val = {0}'.format(ret_val))
        return ret_val

    def _state_run(self):
        '''
        Execute a state run based on information set in the minion config file
        '''
        if self.opts['startup_states']:
            data = {'jid': 'req', 'ret': self.opts.get('ext_job_cache', '')}
            if self.opts['startup_states'] == 'sls':
                data['fun'] = 'state.sls'
                data['arg'] = [self.opts['sls_list']]
            elif self.opts['startup_states'] == 'top':
                data['fun'] = 'state.top'
                data['arg'] = [self.opts['top_file']]
            else:
                data['fun'] = 'state.highstate'
                data['arg'] = []
            self._handle_decoded_payload(data)

    def _refresh_grains_watcher(self, refresh_interval_in_minutes):
        '''
        Create a loop that will fire a pillar refresh to inform a master about a change in the grains of this minion
        :param refresh_interval_in_minutes:
        :return: None
        '''
        if '__update_grains' not in self.opts.get('schedule', {}):
            if 'schedule' not in self.opts:
                self.opts['schedule'] = {}
            self.opts['schedule'].update({
                '__update_grains':
                    {
                        'function': 'event.fire',
                        'args': [{}, 'grains_refresh'],
                        'minutes': refresh_interval_in_minutes
                    }
            })

    def _fire_master_minion_start(self):
        # Send an event to the master that the minion is live
        self._fire_master(
            'Minion {0} started at {1}'.format(
            self.opts['id'],
            time.asctime()
            ),
            'minion_start'
        )
        # dup name spaced event
        self._fire_master(
            'Minion {0} started at {1}'.format(
            self.opts['id'],
            time.asctime()
            ),
            tagify([self.opts['id'], 'start'], 'minion'),
        )

    def module_refresh(self, force_refresh=False, notify=False):
        '''
        Refresh the functions and returners.
        '''
        log.debug('Refreshing modules. Notify={0}'.format(notify))
        if hasattr(self, 'proxy'):
            self.functions, self.returners, _ = self._load_modules(force_refresh, notify=notify, proxy=self.proxy)

            # Proxies have a chicken-and-egg problem.  Usually we load grains early
            # in the setup process, but we can't load grains for proxies until
            # we talk to the device we are proxying for.  So force a grains
            # sync here.
            # Hmm...We can't seem to sync grains here, makes the event bus go nuts
            # leaving this commented to remind future me that this is not a good idea here.
            # self.functions['saltutil.sync_grains'](saltenv='base')
        else:
            self.functions, self.returners, _ = self._load_modules(force_refresh, notify=notify)

        self.schedule.functions = self.functions
        self.schedule.returners = self.returners

    # TODO: only allow one future in flight at a time?
    @tornado.gen.coroutine
    def pillar_refresh(self, force_refresh=False):
        '''
        Refresh the pillar
        '''
        log.debug('Refreshing pillar')
        try:
            self.opts['pillar'] = yield salt.pillar.get_async_pillar(
                self.opts,
                self.opts['grains'],
                self.opts['id'],
                self.opts['environment'],
                pillarenv=self.opts.get('pillarenv'),
            ).compile_pillar()
        except SaltClientError:
            # Do not exit if a pillar refresh fails.
            log.error('Pillar data could not be refreshed. '
                      'One or more masters may be down!')
        self.module_refresh(force_refresh)

    def manage_schedule(self, package):
        '''
        Refresh the functions and returners.
        '''
        tag, data = salt.utils.event.MinionEvent.unpack(package)
        func = data.get('func', None)
        name = data.get('name', None)
        schedule = data.get('schedule', None)
        where = data.get('where', None)
        persist = data.get('persist', None)

        if func == 'delete':
            self.schedule.delete_job(name, persist)
        elif func == 'add':
            self.schedule.add_job(schedule, persist)
        elif func == 'modify':
            self.schedule.modify_job(name, schedule, persist, where)
        elif func == 'enable':
            self.schedule.enable_schedule()
        elif func == 'disable':
            self.schedule.disable_schedule()
        elif func == 'enable_job':
            self.schedule.enable_job(name, persist, where)
        elif func == 'run_job':
            self.schedule.run_job(name)
        elif func == 'disable_job':
            self.schedule.disable_job(name, persist, where)
        elif func == 'reload':
            self.schedule.reload(schedule)
        elif func == 'list':
            self.schedule.list(where)
        elif func == 'save_schedule':
            self.schedule.save_schedule()

    def manage_beacons(self, package):
        '''
        Manage Beacons
        '''
        tag, data = salt.utils.event.MinionEvent.unpack(package)
        func = data.get('func', None)
        name = data.get('name', None)
        beacon_data = data.get('beacon_data', None)

        if func == 'add':
            self.beacons.add_beacon(name, beacon_data)
        elif func == 'modify':
            self.beacons.modify_beacon(name, beacon_data)
        elif func == 'delete':
            self.beacons.delete_beacon(name)
        elif func == 'enable':
            self.beacons.enable_beacons()
        elif func == 'disable':
            self.beacons.disable_beacons()
        elif func == 'enable_beacon':
            self.beacons.enable_beacon(name)
        elif func == 'disable_beacon':
            self.beacons.disable_beacon(name)
        elif func == 'list':
            self.beacons.list_beacons()

    def environ_setenv(self, package):
        '''
        Set the salt-minion main process environment according to
        the data contained in the minion event data
        '''
        tag, data = salt.utils.event.MinionEvent.unpack(package)
        environ = data.get('environ', None)
        if environ is None:
            return False
        false_unsets = data.get('false_unsets', False)
        clear_all = data.get('clear_all', False)
        import salt.modules.environ as mod_environ
        return mod_environ.setenv(environ, false_unsets, clear_all)

    def clean_die(self, signum, frame):
        '''
        Python does not handle the SIGTERM cleanly, if it is signaled exit
        the minion process cleanly
        '''
        self._running = False
        exit(0)

    def _pre_tune(self):
        '''
        Set the minion running flag and issue the appropriate warnings if
        the minion cannot be started or is already running
        '''
        if self._running is None:
            self._running = True
        elif self._running is False:
            log.error(
                'This {0} was scheduled to stop. Not running '
                '{0}.tune_in()'.format(self.__class__.__name__)
            )
            return
        elif self._running is True:
            log.error(
                'This {0} is already running. Not running '
                '{0}.tune_in()'.format(self.__class__.__name__)
            )
            return

        try:
            log.info(
                '{0} is starting as user \'{1}\''.format(
                    self.__class__.__name__,
                    salt.utils.get_user()
                )
            )
        except Exception as err:
            # Only windows is allowed to fail here. See #3189. Log as debug in
            # that case. Else, error.
            log.log(
                salt.utils.is_windows() and logging.DEBUG or logging.ERROR,
                'Failed to get the user who is starting {0}'.format(
                    self.__class__.__name__
                ),
                exc_info=err
            )

    def _mine_send(self, package):
        '''
        Send mine data to the master
        '''
        channel = salt.transport.Channel.factory(self.opts)
        load = salt.utils.event.SaltEvent.unpack(package)[1]
        load['tok'] = self.tok
        try:
            ret = channel.send(load)
            return ret
        except SaltReqTimeoutError:
            log.warning('Unable to send mine data to master.')
            return None

    @tornado.gen.coroutine
    def handle_event(self, package):
        '''
        Handle an event from the epull_sock (all local minion events)
        '''
        log.debug('Handling event {0!r}'.format(package))
        if package.startswith('module_refresh'):
            tag, data = salt.utils.event.MinionEvent.unpack(package)
            self.module_refresh(notify=data.get('notify', False))
        elif package.startswith('pillar_refresh'):
            yield self.pillar_refresh()
        elif package.startswith('manage_schedule'):
            self.manage_schedule(package)
        elif package.startswith('manage_beacons'):
            self.manage_beacons(package)
        elif package.startswith('grains_refresh'):
            if self.grains_cache != self.opts['grains']:
                self.pillar_refresh(force_refresh=True)
                self.grains_cache = self.opts['grains']
        elif package.startswith('environ_setenv'):
            self.environ_setenv(package)
        elif package.startswith('_minion_mine'):
            self._mine_send(package)
        elif package.startswith('fire_master'):
            tag, data = salt.utils.event.MinionEvent.unpack(package)
            log.debug('Forwarding master event tag={tag}'.format(tag=data['tag']))
            self._fire_master(data['data'], data['tag'], data['events'], data['pretag'])
        elif package.startswith('__master_disconnected'):
            tag, data = salt.utils.event.MinionEvent.unpack(package)
            # if the master disconnect event is for a different master, raise an exception
            if data['master'] != self.opts['master']:
                raise Exception()
            if self.connected:
                # we are not connected anymore
                self.connected = False
                # modify the scheduled job to fire only on reconnect
                schedule = {
                   'function': 'status.master',
                   'seconds': self.opts['master_alive_interval'],
                   'jid_include': True,
                   'maxrunning': 2,
                   'kwargs': {'master': self.opts['master'],
                              'connected': False}
                }
                self.schedule.modify_job(name='__master_alive',
                                         schedule=schedule)

                log.info('Connection to master {0} lost'.format(self.opts['master']))

                if self.opts['master_type'] == 'failover':
                    log.info('Trying to tune in to next master from master-list')

                    if hasattr(self, 'pub_channel'):
                        self.pub_channel.on_recv(None)
                        if hasattr(self.pub_channel, 'close'):
                            self.pub_channel.close()
                        del self.pub_channel

                    # if eval_master finds a new master for us, self.connected
                    # will be True again on successful master authentication
                    try:
                        master, self.pub_channel = yield self.eval_master(
                                                            opts=self.opts,
                                                            failed=True)
                    except SaltClientError:
                        pass

                    if self.connected:
                        self.opts['master'] = master

                        # re-init the subsystems to work with the new master
                        log.info('Re-initialising subsystems for new '
                                 'master {0}'.format(self.opts['master']))
                        self.functions, self.returners, self.function_errors = self._load_modules()
                        self.pub_channel.on_recv(self._handle_payload)
                        self._fire_master_minion_start()
                        log.info('Minion is ready to receive requests!')

                        # update scheduled job to run with the new master addr
                        schedule = {
                           'function': 'status.master',
                           'seconds': self.opts['master_alive_interval'],
                           'jid_include': True,
                           'maxrunning': 2,
                           'kwargs': {'master': self.opts['master'],
                                      'connected': True}
                        }
                        self.schedule.modify_job(name='__master_alive',
                                                 schedule=schedule)
                    else:
                        self.restart = True
                        self.io_loop.stop()

        elif package.startswith('__master_connected'):
            # handle this event only once. otherwise it will pollute the log
            if not self.connected:
                log.info('Connection to master {0} re-established'.format(self.opts['master']))
                self.connected = True
                # modify the __master_alive job to only fire,
                # if the connection is lost again
                schedule = {
                   'function': 'status.master',
                   'seconds': self.opts['master_alive_interval'],
                   'jid_include': True,
                   'maxrunning': 2,
                   'kwargs': {'master': self.opts['master'],
                              'connected': True}
                }

                self.schedule.modify_job(name='__master_alive',
                                         schedule=schedule)
        elif package.startswith('_salt_error'):
            tag, data = salt.utils.event.MinionEvent.unpack(package)
            log.debug('Forwarding salt error event tag={tag}'.format(tag=tag))
            self._fire_master(data, tag)
        elif package.startswith('salt/auth/creds'):
            tag, data = salt.utils.event.MinionEvent.unpack(package)
            key = tuple(data['key'])
            log.debug('Updating auth data for {0}: {1} -> {2}'.format(
                    key, salt.crypt.AsyncAuth.creds_map.get(key), data['creds']))
            salt.crypt.AsyncAuth.creds_map[tuple(data['key'])] = data['creds']

    def _fallback_cleanups(self):
        '''
        Fallback cleanup routines, attempting to fix leaked processes, threads, etc.
        '''
        # Add an extra fallback in case a forked process leaks through
        multiprocessing.active_children()

        # Cleanup Windows threads
        if not salt.utils.is_windows():
            return
        for thread in self.win_proc:
            if not thread.is_alive():
                thread.join()
                try:
                    self.win_proc.remove(thread)
                    del thread
                except (ValueError, NameError):
                    pass

    # Main Minion Tune In
    def tune_in(self, start=True):
        '''
        Lock onto the publisher. This is the main event loop for the minion
        :rtype : None
        '''
        self._pre_tune()

        # Properly exit if a SIGTERM is signalled
        signal.signal(signal.SIGTERM, self.clean_die)

        # start up the event publisher, so we can see events during startup
        self.event_publisher = salt.utils.event.AsyncEventPublisher(
            self.opts,
            self.handle_event,
            io_loop=self.io_loop,
        )

        log.debug('Minion {0!r} trying to tune in'.format(self.opts['id']))

        if start:
            self.sync_connect_master()
        if hasattr(self, 'connected') and self.connected:
            self._fire_master_minion_start()
            log.info('Minion is ready to receive requests!')

        # Make sure to gracefully handle SIGUSR1
        enable_sigusr1_handler()

        # Make sure to gracefully handle CTRL_LOGOFF_EVENT
        salt.utils.enable_ctrl_logoff_handler()

        # On first startup execute a state run if configured to do so
        self._state_run()

        loop_interval = self.opts['loop_interval']

        try:
            if self.opts['grains_refresh_every']:  # If exists and is not zero. In minutes, not seconds!
                if self.opts['grains_refresh_every'] > 1:
                    log.debug(
                        'Enabling the grains refresher. Will run every {0} minutes.'.format(
                            self.opts['grains_refresh_every'])
                    )
                else:  # Clean up minute vs. minutes in log message
                    log.debug(
                        'Enabling the grains refresher. Will run every {0} minute.'.format(
                            self.opts['grains_refresh_every'])

                    )
                self._refresh_grains_watcher(
                    abs(self.opts['grains_refresh_every'])
                )
        except Exception as exc:
            log.error(
                'Exception occurred in attempt to initialize grain refresh routine during minion tune-in: {0}'.format(
                    exc)
            )

        self.periodic_callbacks = {}
        # schedule the stuff that runs every interval
        ping_interval = self.opts.get('ping_interval', 0) * 60
        if ping_interval > 0:
            def ping_master():
                try:
                    self._fire_master('ping', 'minion_ping')
                except Exception:
                    log.warning('Attempt to ping master failed.', exc_on_loglevel=logging.DEBUG)
            self.periodic_callbacks['ping'] = tornado.ioloop.PeriodicCallback(ping_master, ping_interval * 1000, io_loop=self.io_loop)

        self.periodic_callbacks['cleanup'] = tornado.ioloop.PeriodicCallback(self._fallback_cleanups, loop_interval * 1000, io_loop=self.io_loop)

        def handle_beacons():
            # Process Beacons
            beacons = None
            try:
                beacons = self.process_beacons(self.functions)
            except Exception:
                log.critical('The beacon errored: ', exc_info=True)
            if beacons:
                self._fire_master(events=beacons)

        self.periodic_callbacks['beacons'] = tornado.ioloop.PeriodicCallback(handle_beacons, loop_interval * 1000, io_loop=self.io_loop)

        # TODO: actually listen to the return and change period
        def handle_schedule():
            self.process_schedule(self, loop_interval)
        if hasattr(self, 'schedule'):
            self.periodic_callbacks['schedule'] = tornado.ioloop.PeriodicCallback(handle_schedule, 1000, io_loop=self.io_loop)

        # start all the other callbacks
        for periodic_cb in six.itervalues(self.periodic_callbacks):
            periodic_cb.start()

        # add handler to subscriber
        if hasattr(self, 'pub_channel'):
            self.pub_channel.on_recv(self._handle_payload)
        else:
            log.error('No connection to master found. Scheduled jobs will not run.')

        if start:
            try:
                self.io_loop.start()
                if self.restart:
                    self.destroy()
            except (KeyboardInterrupt, RuntimeError):  # A RuntimeError can be re-raised by Tornado on shutdown
                self.destroy()

    def _handle_payload(self, payload):
        if payload is not None and payload['enc'] == 'aes':
            if self._target_load(payload['load']):
                self._handle_decoded_payload(payload['load'])
        # If it's not AES, and thus has not been verified, we do nothing.
        # In the future, we could add support for some clearfuncs, but
        # the minion currently has no need.

    def _target_load(self, load):
        # Verify that the publication is valid
        if 'tgt' not in load or 'jid' not in load or 'fun' not in load \
           or 'arg' not in load:
            return False
        # Verify that the publication applies to this minion

        # It's important to note that the master does some pre-processing
        # to determine which minions to send a request to. So for example,
        # a "salt -G 'grain_key:grain_val' test.ping" will invoke some
        # pre-processing on the master and this minion should not see the
        # publication if the master does not determine that it should.

        if 'tgt_type' in load:
            match_func = getattr(self.matcher,
                                 '{0}_match'.format(load['tgt_type']), None)
            if match_func is None:
                return False
            if load['tgt_type'] in ('grain', 'grain_pcre', 'pillar'):
                delimiter = load.get('delimiter', DEFAULT_TARGET_DELIM)
                if not match_func(load['tgt'], delimiter=delimiter):
                    return False
            elif not match_func(load['tgt']):
                return False
        else:
            if not self.matcher.glob_match(load['tgt']):
                return False

        return True

    def destroy(self):
        '''
        Tear down the minion
        '''
        self._running = False
        if hasattr(self, 'pub_channel'):
            self.pub_channel.on_recv(None)
            if hasattr(self.pub_channel, 'close'):
                self.pub_channel.close()
            del self.pub_channel
        if hasattr(self, 'periodic_callbacks'):
            for cb in six.itervalues(self.periodic_callbacks):
                cb.stop()

    def __del__(self):
        self.destroy()


class Syndic(Minion):
    '''
    Make a Syndic minion, this minion will use the minion keys on the
    master to authenticate with a higher level master.
    '''
    def __init__(self, opts, **kwargs):
        self._syndic_interface = opts.get('interface')
        self._syndic = True
        # force auth_safemode True because Syndic don't support autorestart
        opts['auth_safemode'] = True
        opts['loop_interval'] = 1
        super(Syndic, self).__init__(opts, **kwargs)
        self.mminion = salt.minion.MasterMinion(opts)
        self.jid_forward_cache = set()

    def _handle_decoded_payload(self, data):
        '''
        Override this method if you wish to handle the decoded data
        differently.
        '''
        # TODO: even do this??
        data['to'] = int(data.get('to', self.opts['timeout'])) - 1
        # Only forward the command if it didn't originate from ourselves
        if data.get('master_id', 0) != self.opts.get('master_id', 1):
            self.syndic_cmd(data)

    def syndic_cmd(self, data):
        '''
        Take the now clear load and forward it on to the client cmd
        '''
        # Set up default tgt_type
        if 'tgt_type' not in data:
            data['tgt_type'] = 'glob'
        kwargs = {}

        # optionally add a few fields to the publish data
        for field in ('master_id',  # which master the job came from
                      'user',  # which user ran the job
                      ):
            if field in data:
                kwargs[field] = data[field]

        try:
            # Send out the publication
            self.local.pub(data['tgt'],
                           data['fun'],
                           data['arg'],
                           data['tgt_type'],
                           data['ret'],
                           data['jid'],
                           data['to'],
                           **kwargs)
        except Exception as exc:
            log.warning('Unable to forward pub data: {0}'.format(exc))

    def _fire_master_syndic_start(self):
        # Send an event to the master that the minion is live
        self._fire_master(
            'Syndic {0} started at {1}'.format(
            self.opts['id'],
            time.asctime()
            ),
            'syndic_start'
        )
        self._fire_master(
            'Syndic {0} started at {1}'.format(
            self.opts['id'],
            time.asctime()
            ),
            tagify([self.opts['id'], 'start'], 'syndic'),
        )

    # Syndic Tune In
    def tune_in(self, start=True):
        '''
        Lock onto the publisher. This is the main event loop for the syndic
        '''
        signal.signal(signal.SIGTERM, self.clean_die)
        log.debug('Syndic {0!r} trying to tune in'.format(self.opts['id']))

        if start:
            self.sync_connect_master()

        # Instantiate the local client
        self.local = salt.client.get_local_client(self.opts['_minion_conf_file'])
        self.local.event.subscribe('')
        self.local.opts['interface'] = self._syndic_interface

        # add handler to subscriber
        self.pub_channel.on_recv(self._process_cmd_socket)

        # register the event sub to the poller
        self._reset_event_aggregation()
        self.local_event_stream = zmq.eventloop.zmqstream.ZMQStream(self.local.event.sub, io_loop=self.io_loop)
        self.local_event_stream.on_recv(self._process_event)

        # forward events every syndic_event_forward_timeout
        self.forward_events = tornado.ioloop.PeriodicCallback(self._forward_events,
                                                              self.opts['syndic_event_forward_timeout'] * 1000,
                                                              io_loop=self.io_loop)
        self.forward_events.start()

        # Send an event to the master that the minion is live
        self._fire_master_syndic_start()

        # Make sure to gracefully handle SIGUSR1
        enable_sigusr1_handler()

        if start:
            self.io_loop.start()

    # TODO: clean up docs
    def tune_in_no_block(self):
        '''
        Executes the tune_in sequence but omits extra logging and the
        management of the event bus assuming that these are handled outside
        the tune_in sequence
        '''
        # Instantiate the local client
        self.local = salt.client.get_local_client(self.opts['_minion_conf_file'])

        # add handler to subscriber
        self.pub_channel.on_recv(self._process_cmd_socket)

    def _process_cmd_socket(self, payload):
        if payload is not None and payload['enc'] == 'aes':
            log.trace('Handling payload')
            self._handle_decoded_payload(payload['load'])
        # If it's not AES, and thus has not been verified, we do nothing.
        # In the future, we could add support for some clearfuncs, but
        # the syndic currently has no need.

    def _reset_event_aggregation(self):
        self.jids = {}
        self.raw_events = []

    def _process_event(self, raw):
        # TODO: cleanup: Move down into event class
        raw = raw[0]
        mtag, data = self.local.event.unpack(raw, self.local.event.serial)
        event = {'data': data, 'tag': mtag}
        log.trace('Got event {0}'.format(event['tag']))
        tag_parts = event['tag'].split('/')
        if len(tag_parts) >= 4 and tag_parts[1] == 'job' and \
            salt.utils.jid.is_jid(tag_parts[2]) and tag_parts[3] == 'ret' and \
            'return' in event['data']:
            if 'jid' not in event['data']:
                # Not a job return
                return
            jdict = self.jids.setdefault(event['data']['jid'], {})
            if not jdict:
                jdict['__fun__'] = event['data'].get('fun')
                jdict['__jid__'] = event['data']['jid']
                jdict['__load__'] = {}
                fstr = '{0}.get_load'.format(self.opts['master_job_cache'])
                # Only need to forward each load once. Don't hit the disk
                # for every minion return!
                if event['data']['jid'] not in self.jid_forward_cache:
                    jdict['__load__'].update(
                        self.mminion.returners[fstr](event['data']['jid'])
                        )
                    self.jid_forward_cache.add(event['data']['jid'])
                    if len(self.jid_forward_cache) > self.opts['syndic_jid_forward_cache_hwm']:
                        # Pop the oldest jid from the cache
                        tmp = sorted(list(self.jid_forward_cache))
                        tmp.pop(0)
                        self.jid_forward_cache = set(tmp)
            if 'master_id' in event['data']:
                # __'s to make sure it doesn't print out on the master cli
                jdict['__master_id__'] = event['data']['master_id']
            jdict[event['data']['id']] = event['data']['return']
        else:
            # Add generic event aggregation here
            if 'retcode' not in event['data']:
                self.raw_events.append(event)

    def _forward_events(self):
        log.trace('Forwarding events')
        if self.raw_events:
            self._fire_master(events=self.raw_events,
                              pretag=tagify(self.opts['id'], base='syndic'),
                              )
        for jid in self.jids:
            self._return_pub(self.jids[jid],
                             '_syndic_return',
                             timeout=self._return_retry_timer())
        self._reset_event_aggregation()

    def destroy(self):
        '''
        Tear down the syndic minion
        '''
        # We borrowed the local clients poller so give it back before
        # it's destroyed. Reset the local poller reference.
        super(Syndic, self).destroy()
        if hasattr(self, 'local'):
            del self.local

        if hasattr(self, 'forward_events'):
            self.forward_events.stop()


# TODO: consolidate syndic classes together?
# need a way of knowing if the syndic connection is busted
class MultiSyndic(MinionBase):
    '''
    Make a MultiSyndic minion, this minion will handle relaying jobs and returns from
    all minions connected to it to the list of masters it is connected to.

    Modes (controlled by `syndic_mode`:
        sync: This mode will synchronize all events and publishes from higher level masters
        cluster: This mode will only sync job publishes and returns

    Note: jobs will be returned best-effort to the requesting master. This also means
    (since we are using zmq) that if a job was fired and the master disconnects
    between the publish and return, that the return will end up in a zmq buffer
    in this Syndic headed to that original master.

    In addition, since these classes all seem to use a mix of blocking and non-blocking
    calls (with varying timeouts along the way) this daemon does not handle failure well,
    it will (under most circumstances) stall the daemon for ~15s trying to forward events
    to the down master
    '''
    # time to connect to upstream master
    SYNDIC_CONNECT_TIMEOUT = 5
    SYNDIC_EVENT_TIMEOUT = 5

    def __init__(self, opts, io_loop=None):
        opts['loop_interval'] = 1
        super(MultiSyndic, self).__init__(opts)
        self.mminion = salt.minion.MasterMinion(opts)
        # sync (old behavior), cluster (only returns and publishes)
        self.syndic_mode = self.opts.get('syndic_mode', 'sync')

        self.auth_wait = self.opts['acceptance_wait_time']
        self.max_auth_wait = self.opts['acceptance_wait_time_max']

        self._has_master = threading.Event()
        self.jid_forward_cache = set()

        if io_loop is None:
            self.io_loop = zmq.eventloop.ioloop.ZMQIOLoop()
        else:
            self.io_loop = io_loop
        self.io_loop.install()

    def _spawn_syndics(self):
        '''
        Spawn all the coroutines which will sign in the syndics
        '''
        self._syndics = {}  # mapping of opts['master'] -> syndic
        for master in set(self.opts['master']):
            s_opts = copy.copy(self.opts)
            s_opts['master'] = master
            self._syndics[master] = self._connect_syndic(s_opts)

    @tornado.gen.coroutine
    def _connect_syndic(self, opts):
        '''
        Create a syndic, and asynchronously connect it to a master
        '''
        last = 0  # never have we signed in
        auth_wait = opts['acceptance_wait_time']
        while True:
            log.debug('Syndic attempting to connect to {0}'.format(opts['master']))
            try:
                syndic = Syndic(opts,
                                timeout=self.SYNDIC_CONNECT_TIMEOUT,
                                safe=False,
                                io_loop=self.io_loop,
                                )
                yield syndic.connect_master()
                # set up the syndic to handle publishes (specifically not event forwarding)
                syndic.tune_in_no_block()
                log.info('Syndic successfully connected to {0}'.format(opts['master']))
                break
            except SaltClientError as exc:
                log.error('Error while bringing up syndic for multi-syndic. Is master at {0} responding?'.format(opts['master']))
                last = time.time()
                if auth_wait < self.max_auth_wait:
                    auth_wait += self.auth_wait
                yield tornado.gen.sleep(auth_wait)  # TODO: log?
            except KeyboardInterrupt:
                raise
            except:  # pylint: disable=W0702
                log.critical('Unexpected error while connecting to {0}'.format(opts['master']), exc_info=True)

        raise tornado.gen.Return(syndic)

    def _mark_master_dead(self, master):
        '''
        Mark a master as dead. This will start the sign-in routine
        '''
        # if its connected, mark it dead
        if self._syndics[master].done():
            syndic = self._syndics.result()
            syndic.destroy()
            self._syndics[master] = self._connect_syndic(syndic.opts)
        else:
            log.info('Attempting to mark {0} as dead, although it is already marked dead'.format(master))  # TODO: debug?

    def _call_syndic(self, func, args=(), kwargs=None, master_id=None):
        '''
        Wrapper to call a given func on a syndic, best effort to get the one you asked for
        '''
        if kwargs is None:
            kwargs = {}
        for master, syndic_future in self.iter_master_options(master_id):
            if not syndic_future.done() or syndic_future.exception():
                log.error('Unable to call {0} on {1}, that syndic is not connected'.format(func, master_id))
                continue

            try:
                getattr(syndic_future.result(), func)(*args, **kwargs)
                return
            except SaltClientError:
                log.error('Unable to call {0} on {1}, trying another...'.format(func, master_id))
                self._mark_master_dead(master)
                continue
        log.critical('Unable to call {0} on any masters!'.format(func))

    def iter_master_options(self, master_id=None):
        '''
        Iterate (in order) over your options for master
        '''
        masters = list(self._syndics.keys())
        shuffle(masters)
        if master_id not in self._syndics:
            master_id = masters.pop(0)
        else:
            masters.remove(master_id)

        while True:
            yield master_id, self._syndics[master_id]
            if len(masters) == 0:
                break
            master_id = masters.pop(0)

    def _reset_event_aggregation(self):
        self.jids = {}
        self.raw_events = []

    # Syndic Tune In
    def tune_in(self):
        '''
        Lock onto the publisher. This is the main event loop for the syndic
        '''
        self._spawn_syndics()
        # Instantiate the local client
        self.local = salt.client.get_local_client(self.opts['_minion_conf_file'])
        self.local.event.subscribe('')

        log.debug('MultiSyndic {0!r} trying to tune in'.format(self.opts['id']))

        # register the event sub to the poller
        self._reset_event_aggregation()
        self.local_event_stream = zmq.eventloop.zmqstream.ZMQStream(self.local.event.sub, io_loop=self.io_loop)
        self.local_event_stream.on_recv(self._process_event)

        # forward events every syndic_event_forward_timeout
        self.forward_events = tornado.ioloop.PeriodicCallback(self._forward_events,
                                                              self.opts['syndic_event_forward_timeout'] * 1000,
                                                              io_loop=self.io_loop)
        self.forward_events.start()

        # Make sure to gracefully handle SIGUSR1
        enable_sigusr1_handler()

        self.io_loop.start()

    def _process_event(self, raw):
        # TODO: cleanup: Move down into event class
        raw = raw[0]
        mtag, data = self.local.event.unpack(raw, self.local.event.serial)
        event = {'data': data, 'tag': mtag}
        log.trace('Got event {0}'.format(event['tag']))

        tag_parts = event['tag'].split('/')
        if len(tag_parts) >= 4 and tag_parts[1] == 'job' and \
            salt.utils.jid.is_jid(tag_parts[2]) and tag_parts[3] == 'ret' and \
            'return' in event['data']:
            if 'jid' not in event['data']:
                # Not a job return
                return
            if self.syndic_mode == 'cluster' and event['data'].get('master_id', 0) == self.opts.get('master_id', 1):
                log.debug('Return received with matching master_id, not forwarding')
                return

            jdict = self.jids.setdefault(event['tag'], {})
            if not jdict:
                jdict['__fun__'] = event['data'].get('fun')
                jdict['__jid__'] = event['data']['jid']
                jdict['__load__'] = {}
                fstr = '{0}.get_load'.format(self.opts['master_job_cache'])
                # Only need to forward each load once. Don't hit the disk
                # for every minion return!
                if event['data']['jid'] not in self.jid_forward_cache:
                    jdict['__load__'].update(
                        self.mminion.returners[fstr](event['data']['jid'])
                        )
                    self.jid_forward_cache.add(event['data']['jid'])
                    if len(self.jid_forward_cache) > self.opts['syndic_jid_forward_cache_hwm']:
                        # Pop the oldest jid from the cache
                        tmp = sorted(list(self.jid_forward_cache))
                        tmp.pop(0)
                        self.jid_forward_cache = set(tmp)
            if 'master_id' in event['data']:
                # __'s to make sure it doesn't print out on the master cli
                jdict['__master_id__'] = event['data']['master_id']
            jdict[event['data']['id']] = event['data']['return']
        else:
            # TODO: config to forward these? If so we'll have to keep track of who
            # has seen them
            # if we are the top level masters-- don't forward all the minion events
            if self.syndic_mode == 'sync':
                # Add generic event aggregation here
                if 'retcode' not in event['data']:
                    self.raw_events.append(event)

    def _forward_events(self):
        log.trace('Forwarding events')
        if self.raw_events:
            self._call_syndic('_fire_master',
                              kwargs={'events': self.raw_events,
                                      'pretag': tagify(self.opts['id'], base='syndic'),
                                      'timeout': self.SYNDIC_EVENT_TIMEOUT,
                                      },
                              )
        for jid, jid_ret in self.jids.items():
            self._call_syndic('_return_pub',
                              args=(jid_ret, '_syndic_return'),
                              kwargs={'timeout': self.SYNDIC_EVENT_TIMEOUT},
                              master_id=jid_ret.get('__master_id__'),
                              )

        self._reset_event_aggregation()


class Matcher(object):
    '''
    Use to return the value for matching calls from the master
    '''
    def __init__(self, opts, functions=None):
        self.opts = opts
        self.functions = functions

    def confirm_top(self, match, data, nodegroups=None):
        '''
        Takes the data passed to a top file environment and determines if the
        data matches this minion
        '''
        matcher = 'compound'
        if not data:
            log.error('Received bad data when setting the match from the top '
                      'file')
            return False
        for item in data:
            if isinstance(item, dict):
                if 'match' in item:
                    matcher = item['match']
        if hasattr(self, matcher + '_match'):
            funcname = '{0}_match'.format(matcher)
            if matcher == 'nodegroup':
                return getattr(self, funcname)(match, nodegroups)
            return getattr(self, funcname)(match)
        else:
            log.error('Attempting to match with unknown matcher: {0}'.format(
                matcher
            ))
            return False

    def glob_match(self, tgt):
        '''
        Returns true if the passed glob matches the id
        '''
        if not isinstance(tgt, six.string_types):
            return False

        return fnmatch.fnmatch(self.opts['id'], tgt)

    def pcre_match(self, tgt):
        '''
        Returns true if the passed pcre regex matches
        '''
        return bool(re.match(tgt, self.opts['id']))

    def list_match(self, tgt):
        '''
        Determines if this host is on the list
        '''
        if isinstance(tgt, six.string_types):
            tgt = tgt.split(',')
        return bool(self.opts['id'] in tgt)

    def grain_match(self, tgt, delimiter=DEFAULT_TARGET_DELIM):
        '''
        Reads in the grains glob match
        '''
        log.debug('grains target: {0}'.format(tgt))
        if delimiter not in tgt:
            log.error('Got insufficient arguments for grains match '
                      'statement from master')
            return False
        return salt.utils.subdict_match(
            self.opts['grains'], tgt, delimiter=delimiter
        )

    def grain_pcre_match(self, tgt, delimiter=DEFAULT_TARGET_DELIM):
        '''
        Matches a grain based on regex
        '''
        log.debug('grains pcre target: {0}'.format(tgt))
        if delimiter not in tgt:
            log.error('Got insufficient arguments for grains pcre match '
                      'statement from master')
            return False
        return salt.utils.subdict_match(self.opts['grains'], tgt,
                                        delimiter=delimiter, regex_match=True)

    def data_match(self, tgt):
        '''
        Match based on the local data store on the minion
        '''
        if self.functions is None:
            utils = salt.loader.utils(self.opts)
            self.functions = salt.loader.minion_mods(self.opts, utils=utils)
        comps = tgt.split(':')
        if len(comps) < 2:
            return False
        val = self.functions['data.getval'](comps[0])
        if val is None:
            # The value is not defined
            return False
        if isinstance(val, list):
            # We are matching a single component to a single list member
            for member in val:
                if fnmatch.fnmatch(str(member).lower(), comps[1].lower()):
                    return True
            return False
        if isinstance(val, dict):
            if comps[1] in val:
                return True
            return False
        return bool(fnmatch.fnmatch(
            val,
            comps[1],
        ))

    def pillar_match(self, tgt, delimiter=DEFAULT_TARGET_DELIM):
        '''
        Reads in the pillar glob match
        '''
        log.debug('pillar target: {0}'.format(tgt))
        if delimiter not in tgt:
            log.error('Got insufficient arguments for pillar match '
                      'statement from master')
            return False
        return salt.utils.subdict_match(
            self.opts['pillar'], tgt, delimiter=delimiter
        )

    def pillar_pcre_match(self, tgt, delimiter=DEFAULT_TARGET_DELIM):
        '''
        Reads in the pillar pcre match
        '''
        log.debug('pillar PCRE target: {0}'.format(tgt))
        if delimiter not in tgt:
            log.error('Got insufficient arguments for pillar PCRE match '
                      'statement from master')
            return False
        return salt.utils.subdict_match(
            self.opts['pillar'], tgt, delimiter=delimiter, regex_match=True
        )

    def pillar_exact_match(self, tgt, delimiter=':'):
        '''
        Reads in the pillar match, no globbing, no PCRE
        '''
        log.debug('pillar target: {0}'.format(tgt))
        if delimiter not in tgt:
            log.error('Got insufficient arguments for pillar match '
                      'statement from master')
            return False
        return salt.utils.subdict_match(self.opts['pillar'],
                                        tgt,
                                        delimiter=delimiter,
                                        exact_match=True)

    def ipcidr_match(self, tgt):
        '''
        Matches based on IP address or CIDR notation
        '''
        try:
            # Target is an address?
            tgt = ipaddress.ip_address(tgt)
        except:  # pylint: disable=bare-except
            try:
                # Target is a network?
                tgt = ipaddress.ip_network(tgt)
            except:  # pylint: disable=bare-except
                log.error('Invalid IP/CIDR target: {0}'.format(tgt))
                return []
        proto = 'ipv{0}'.format(tgt.version)

        grains = self.opts['grains']

        if proto not in grains:
            match = False
        elif isinstance(tgt, (ipaddress.IPv4Address, ipaddress.IPv6Address)):
            match = str(tgt) in grains[proto]
        else:
            match = salt.utils.network.in_subnet(tgt, grains[proto])

        return match

    def range_match(self, tgt):
        '''
        Matches based on range cluster
        '''
        if HAS_RANGE:
            range_ = seco.range.Range(self.opts['range_server'])
            try:
                return self.opts['grains']['fqdn'] in range_.expand(tgt)
            except seco.range.RangeException as exc:
                log.debug('Range exception in compound match: {0}'.format(exc))
                return False
        return False

    def compound_match(self, tgt):
        '''
        Runs the compound target check
        '''
        if not isinstance(tgt, six.string_types) and not isinstance(tgt, (list, tuple)):
            log.error('Compound target received that is neither string, list nor tuple')
            return False
        log.debug('compound_match: {0} ? {1}'.format(self.opts['id'], tgt))
        ref = {'G': 'grain',
               'P': 'grain_pcre',
               'I': 'pillar',
               'J': 'pillar_pcre',
               'L': 'list',
               'N': None,      # Nodegroups should already be expanded
               'S': 'ipcidr',
               'E': 'pcre'}
        if HAS_RANGE:
            ref['R'] = 'range'

        results = []
        opers = ['and', 'or', 'not', '(', ')']

        if isinstance(tgt, six.string_types):
            words = tgt.split()
        else:
            words = tgt

        for word in words:
            target_info = salt.utils.minions.parse_target(word)

            # Easy check first
            if word in opers:
                if results:
                    if results[-1] == '(' and word in ('and', 'or'):
                        log.error('Invalid beginning operator after "(": {0}'.format(word))
                        return False
                    if word == 'not':
                        if not results[-1] in ('and', 'or', '('):
                            results.append('and')
                    results.append(word)
                else:
                    # seq start with binary oper, fail
                    if word not in ['(', 'not']:
                        log.error('Invalid beginning operator: {0}'.format(word))
                        return False
                    results.append(word)

            elif target_info and target_info['engine']:
                if 'N' == target_info['engine']:
                    # Nodegroups should already be expanded/resolved to other engines
                    log.error('Detected nodegroup expansion failure of "{0}"'.format(word))
                    return False
                engine = ref.get(target_info['engine'])
                if not engine:
                    # If an unknown engine is called at any time, fail out
                    log.error('Unrecognized target engine "{0}" for'
                              ' target expression "{1}"'.format(
                                  target_info['engine'],
                                  word,
                                )
                        )
                    return False

                engine_args = [target_info['pattern']]
                engine_kwargs = {}
                if target_info['delimiter']:
                    engine_kwargs['delimiter'] = target_info['delimiter']

                results.append(
                    str(getattr(self, '{0}_match'.format(engine))(*engine_args, **engine_kwargs))
                )

            else:
                # The match is not explicitly defined, evaluate it as a glob
                results.append(str(self.glob_match(word)))

        results = ' '.join(results)
        log.debug('compound_match {0} ? "{1}" => "{2}"'.format(self.opts['id'], tgt, results))
        try:
            return eval(results)  # pylint: disable=W0123
        except Exception:
            log.error('Invalid compound target: {0} for results: {1}'.format(tgt, results))
            return False
        return False

    def nodegroup_match(self, tgt, nodegroups):
        '''
        This is a compatibility matcher and is NOT called when using
        nodegroups for remote execution, but is called when the nodegroups
        matcher is used in states
        '''
        if tgt in nodegroups:
            return self.compound_match(
                salt.utils.minions.nodegroup_comp(tgt, nodegroups)
            )
        return False


class ProxyMinion(Minion):
    '''
    This class instantiates a 'proxy' minion--a minion that does not manipulate
    the host it runs on, but instead manipulates a device that cannot run a minion.
    '''

    # TODO: better name...
    @tornado.gen.coroutine
    def _post_master_init(self, master):
        '''
        Function to finish init after connecting to a master

        This is primarily loading modules, pillars, etc. (since they need
        to know which master they connected to)
        '''
        log.debug("subclassed _post_master_init")

        self.opts['master'] = master

        self.opts['pillar'] = yield salt.pillar.get_async_pillar(
            self.opts,
            self.opts['grains'],
            self.opts['id'],
            self.opts['environment'],
            pillarenv=self.opts.get('pillarenv'),
        ).compile_pillar()

        if 'proxy' not in self.opts['pillar']:
            log.error('No proxy key found in pillar for id '+self.opts['id']+'.')
            log.error('Check your pillar configuration and contents.  Salt-proxy aborted.')
            self._running = False
            raise SaltSystemExit(code=-1)

        fq_proxyname = self.opts['pillar']['proxy']['proxytype']
        self.opts['proxy'] = self.opts['pillar']['proxy']

        # Need to load the modules so they get all the dunder variables
        self.functions, self.returners, self.function_errors = self._load_modules()

        # we can then sync any proxymodules down from the master
        self.functions['saltutil.sync_proxymodules'](saltenv='base')

        # Then load the proxy module
        self.proxy = salt.loader.proxy(self.opts)

        # Check config 'add_proxymodule_to_opts'  Remove this in Boron.
        if self.opts['add_proxymodule_to_opts']:
            self.opts['proxymodule'] = self.proxy

        # And re-load the modules so the __proxy__ variable gets injected
        self.functions, self.returners, self.function_errors = self._load_modules(proxy=self.proxy)
        self.functions.pack['__proxy__'] = self.proxy
        self.proxy.pack['__salt__'] = self.functions
        self.proxy.pack['__ret__'] = self.returners
        self.proxy.pack['__pillar__'] = self.opts['pillar']

        if ('{0}.init'.format(fq_proxyname) not in self.proxy
            or '{0}.shutdown'.format(fq_proxyname) not in self.proxy):
            log.error('Proxymodule {0} is missing an init() or a shutdown() or both.'.format(fq_proxyname))
            log.error('Check your proxymodule.  Salt-proxy aborted.')
            self._running = False
            raise SaltSystemExit(code=-1)

        proxy_init_fn = self.proxy[fq_proxyname+'.init']
        proxy_init_fn(self.opts)

        # Proxies have a chicken-and-egg problem.  Usually we load grains early
        # in the setup process, but we can't load grains for proxies until
        # we talk to the device we are proxying for.  So reload the grains
        # functions here, and then force a grains sync in modules_refresh
        self.opts['grains'] = salt.loader.grains(self.opts, force_refresh=True)

        self.serial = salt.payload.Serial(self.opts)
        self.mod_opts = self._prep_mod_opts()
        self.matcher = Matcher(self.opts, self.functions)
        self.beacons = salt.beacons.Beacon(self.opts, self.functions)
        uid = salt.utils.get_uid(user=self.opts.get('user', None))
        self.proc_dir = get_proc_dir(self.opts['cachedir'], uid=uid)

        self.schedule = salt.utils.schedule.Schedule(
            self.opts,
            self.functions,
            self.returners)

        # add default scheduling jobs to the minions scheduler
        if self.opts['mine_enabled'] and 'mine.update' in self.functions:
            self.schedule.add_job({
                '__mine_interval':
                    {
                        'function': 'mine.update',
                        'minutes': self.opts['mine_interval'],
                        'jid_include': True,
                        'maxrunning': 2
                    }
            }, persist=True)

        # add master_alive job if enabled
        if self.opts['master_alive_interval'] > 0:
            self.schedule.add_job({
                '__master_alive':
                    {
                        'function': 'status.master',
                        'seconds': self.opts['master_alive_interval'],
                        'jid_include': True,
                        'maxrunning': 1,
                        'kwargs': {'master': self.opts['master'],
                                   'connected': True}
                    }
            }, persist=True)

        self.grains_cache = self.opts['grains']
