# -*- coding: utf-8 -*-
'''
    :codeauthor: :email:`Rahul Handay <rahulha@saltstack.com>`
'''

# Import Python libs
from __future__ import absolute_import
import os

# Import Salt Testing Libs
from salt.exceptions import CommandExecutionError
from salttesting import TestCase, skipIf
from salttesting.helpers import ensure_in_syspath
from salttesting.mock import (
    MagicMock,
    patch,
    NO_MOCK,
    NO_MOCK_REASON
)

ensure_in_syspath('../../')

# Import Salt Libs
from salt.modules import systemd
import salt.utils.systemd

# Globals
systemd.__salt__ = {}
systemd.__context__ = {}

_SYSTEMCTL_STATUS = {
    'sshd.service': '''\
* sshd.service - OpenSSH Daemon
   Loaded: loaded (/usr/lib/systemd/system/sshd.service; disabled; vendor preset: disabled)
   Active: inactive (dead)''',

    'foo.service': '''\
* foo.service
   Loaded: not-found (Reason: No such file or directory)
   Active: inactive (dead)'''
}

_LIST_UNIT_FILES = '''\
service1.service                           enabled
service2.service                           disabled
service3.service                           static
timer1.timer                               enabled
timer2.timer                               disabled
timer3.timer                               static'''


@skipIf(NO_MOCK, NO_MOCK_REASON)
class SystemdTestCase(TestCase):
    '''
        Test case for salt.modules.systemd
    '''
    def test_systemctl_reload(self):
        '''
            Test to Reloads systemctl
        '''
        mock = MagicMock(side_effect=[
            {'stdout': 'Who knows why?',
             'stderr': '',
             'retcode': 1,
             'pid': 12345},
            {'stdout': '',
             'stderr': '',
             'retcode': 0,
             'pid': 54321},
        ])
        with patch.dict(systemd.__salt__, {'cmd.run_all': mock}):
            self.assertRaisesRegexp(
                CommandExecutionError,
                'Problem performing systemctl daemon-reload: Who knows why?',
                systemd.systemctl_reload
            )
            self.assertTrue(systemd.systemctl_reload())

    def test_get_enabled(self):
        '''
        Test to return a list of all enabled services
        '''
        cmd_mock = MagicMock(return_value=_LIST_UNIT_FILES)
        listdir_mock = MagicMock(return_value=['foo', 'bar', 'baz', 'README'])
        sd_mock = MagicMock(
            return_value=set(
                [x.replace('.service', '') for x in _SYSTEMCTL_STATUS]
            )
        )
        access_mock = MagicMock(
            side_effect=lambda x, y: x != os.path.join(
                systemd.INITSCRIPT_PATH,
                'README'
            )
        )
        sysv_enabled_mock = MagicMock(side_effect=lambda x: x == 'baz')

        with patch.dict(systemd.__salt__, {'cmd.run': cmd_mock}):
            with patch.object(os, 'listdir', listdir_mock):
                with patch.object(systemd, '_get_systemd_services', sd_mock):
                    with patch.object(os, 'access', side_effect=access_mock):
                        with patch.object(systemd, '_sysv_enabled',
                                          sysv_enabled_mock):
                            self.assertListEqual(
                                systemd.get_enabled(),
                                ['baz', 'service1', 'timer1.timer']
                            )

    def test_get_disabled(self):
        '''
        Test to return a list of all disabled services
        '''
        cmd_mock = MagicMock(return_value=_LIST_UNIT_FILES)
        # 'foo' should collide with the systemd services (as returned by
        # sd_mock) and thus not be returned by _get_sysv_services(). It doesn't
        # matter that it's not part of the _LIST_UNIT_FILES output, we just
        # want to ensure that 'foo' isn't identified as a disabled initscript
        # even though below we are mocking it to show as not enabled (since
        # only 'baz' will be considered an enabled sysv service).
        listdir_mock = MagicMock(return_value=['foo', 'bar', 'baz', 'README'])
        sd_mock = MagicMock(
            return_value=set(
                [x.replace('.service', '') for x in _SYSTEMCTL_STATUS]
            )
        )
        access_mock = MagicMock(
            side_effect=lambda x, y: x != os.path.join(
                systemd.INITSCRIPT_PATH,
                'README'
            )
        )
        sysv_enabled_mock = MagicMock(side_effect=lambda x: x == 'baz')

        with patch.dict(systemd.__salt__, {'cmd.run': cmd_mock}):
            with patch.object(os, 'listdir', listdir_mock):
                with patch.object(systemd, '_get_systemd_services', sd_mock):
                    with patch.object(os, 'access', side_effect=access_mock):
                        with patch.object(systemd, '_sysv_enabled',
                                          sysv_enabled_mock):
                            self.assertListEqual(
                                systemd.get_disabled(),
                                ['bar', 'service2', 'timer2.timer']
                            )

    def test_get_all(self):
        '''
        Test to return a list of all available services
        '''
        listdir_mock = MagicMock(side_effect=[
            ['foo.service', 'multi-user.target.wants', 'mytimer.timer'],
            ['foo.service', 'multi-user.target.wants', 'bar.service'],
            ['mysql', 'nginx', 'README'],
            ['mysql', 'nginx', 'README']
        ])
        access_mock = MagicMock(
            side_effect=lambda x, y: x != os.path.join(
                systemd.INITSCRIPT_PATH,
                'README'
            )
        )
        with patch.object(os, 'listdir', listdir_mock):
            with patch.object(os, 'access', side_effect=access_mock):
                self.assertListEqual(
                    systemd.get_all(),
                    ['bar', 'foo', 'mysql', 'mytimer.timer', 'nginx']
                )

    def test_available(self):
        '''
        Test to check that the given service is available
        '''
        mock = MagicMock(side_effect=lambda x: _SYSTEMCTL_STATUS[x])
        with patch.object(systemd, '_systemctl_status', mock):
            self.assertTrue(systemd.available('sshd.service'))
            self.assertFalse(systemd.available('foo.service'))

    def test_missing(self):
        '''
            Test to the inverse of service.available.
        '''
        mock = MagicMock(side_effect=lambda x: _SYSTEMCTL_STATUS[x])
        with patch.object(systemd, '_systemctl_status', mock):
            self.assertFalse(systemd.missing('sshd.service'))
            self.assertTrue(systemd.missing('foo.service'))

    def test_show(self):
        '''
            Test to show properties of one or more units/jobs or the manager
        '''
        mock = MagicMock(return_value="a = b , c = d")
        with patch.dict(systemd.__salt__, {'cmd.run': mock}):
            self.assertDictEqual(systemd.show("sshd"), {'a ': ' b , c = d'})

    def test_execs(self):
        '''
            Test to return a list of all files specified as ``ExecStart``
            for all services
        '''
        mock = MagicMock(return_value=["a", "b"])
        with patch.object(systemd, 'get_all', mock):
            mock = MagicMock(return_value={"ExecStart": {"path": "c"}})
            with patch.object(systemd, 'show', mock):
                self.assertDictEqual(systemd.execs(), {'a': 'c', 'b': 'c'})


@skipIf(NO_MOCK, NO_MOCK_REASON)
class SystemdScopeTestCase(TestCase):
    '''
        Test case for salt.modules.systemd, for functions which use systemd
        scopes
    '''
    unit_name = 'foo'
    mock_none = MagicMock(return_value=None)
    mock_success = MagicMock(return_value=0)
    mock_failure = MagicMock(return_value=1)
    mock_true = MagicMock(return_value=True)
    mock_false = MagicMock(return_value=False)
    mock_empty_list = MagicMock(return_value=[])
    mock_run_all_success = MagicMock(return_value={'retcode': 0,
                                                   'stdout': '',
                                                   'stderr': '',
                                                   'pid': 12345})
    mock_run_all_failure = MagicMock(return_value={'retcode': 1,
                                                   'stdout': '',
                                                   'stderr': '',
                                                   'pid': 12345})

    def _change_state(self, action):
        '''
        Common code for start/stop/restart/reload/force_reload tests
        '''
        # We want the traceback if the function name can't be found in the
        # systemd execution module.
        func = getattr(systemd, action)
        # Remove trailing _ in "reload_"
        action = action.rstrip('_').replace('_', '-')
        systemctl_command = ['systemctl', action, self.unit_name + '.service']

        assert_kwargs = {'python_shell': False}
        if action in ('enable', 'disable'):
            assert_kwargs['ignore_retcode'] = True

        with patch.object(systemd, '_check_for_unit_changes', self.mock_none):
            with patch.object(systemd, '_unit_file_changed', self.mock_none):
                with patch.object(systemd, '_get_sysv_services', self.mock_empty_list):
                    with patch.object(systemd, 'unmask', self.mock_true):

                        # Has scopes available
                        with patch.object(salt.utils.systemd, 'has_scope', self.mock_true):

                            # Scope enabled, successful
                            with patch.dict(
                                    systemd.__salt__,
                                    {'config.get': self.mock_true,
                                     'cmd.retcode': self.mock_success}):
                                ret = func(self.unit_name)
                                self.assertTrue(ret)
                                self.mock_success.assert_called_with(
                                    ['systemd-run', '--scope'] + systemctl_command,
                                    **assert_kwargs)

                            # Scope enabled, failed
                            with patch.dict(
                                    systemd.__salt__,
                                    {'config.get': self.mock_true,
                                     'cmd.retcode': self.mock_failure}):
                                ret = func(self.unit_name)
                                self.assertFalse(ret)
                                self.mock_failure.assert_called_with(
                                    ['systemd-run', '--scope'] + systemctl_command,
                                    **assert_kwargs)

                            # Scope disabled, successful
                            with patch.dict(
                                    systemd.__salt__,
                                    {'config.get': self.mock_false,
                                     'cmd.retcode': self.mock_success}):
                                ret = func(self.unit_name)
                                self.assertTrue(ret)
                                self.mock_success.assert_called_with(
                                    systemctl_command,
                                    **assert_kwargs)

                            # Scope disabled, failed
                            with patch.dict(
                                    systemd.__salt__,
                                    {'config.get': self.mock_false,
                                     'cmd.retcode': self.mock_failure}):
                                ret = func(self.unit_name)
                                self.assertFalse(ret)
                                self.mock_failure.assert_called_with(
                                    systemctl_command,
                                    **assert_kwargs)

                        # Does not have scopes available
                        with patch.object(salt.utils.systemd, 'has_scope', self.mock_false):

                            # The results should be the same irrespective of
                            # whether or not scope is enabled, since scope is not
                            # available, so we repeat the below tests with it both
                            # enabled and disabled.
                            for scope_mock in (self.mock_true, self.mock_false):

                                # Successful
                                with patch.dict(
                                        systemd.__salt__,
                                        {'config.get': scope_mock,
                                         'cmd.retcode': self.mock_success}):
                                    ret = func(self.unit_name)
                                    self.assertTrue(ret)
                                    self.mock_success.assert_called_with(
                                        systemctl_command,
                                        **assert_kwargs)

                                # Failed
                                with patch.dict(
                                        systemd.__salt__,
                                        {'config.get': scope_mock,
                                         'cmd.retcode': self.mock_failure}):
                                    ret = func(self.unit_name)
                                    self.assertFalse(ret)
                                    self.mock_failure.assert_called_with(
                                        systemctl_command,
                                        **assert_kwargs)

    def _mask_unmask(self, action, runtime):
        '''
        Common code for mask/unmask tests
        '''
        # We want the traceback if the function name can't be found in the
        # systemd execution module.
        func = getattr(systemd, action)
        systemctl_command = ['systemctl', action]
        if runtime:
            systemctl_command.append('--runtime')
        systemctl_command.append(self.unit_name + '.service')

        args = [self.unit_name]
        if action != 'unmask':
            # We don't need to pass a runtime arg if we're testing unmask(),
            # because unmask() automagically figures out whether or not we're
            # unmasking a runtime-masked service.
            args.append(runtime)

        masked_mock = MagicMock(
            return_value='masked-runtime' if runtime else 'masked')

        with patch.object(systemd, '_check_for_unit_changes', self.mock_none):
            with patch.object(systemd, 'masked', masked_mock):

                # Has scopes available
                with patch.object(salt.utils.systemd, 'has_scope', self.mock_true):

                    # Scope enabled, successful
                    with patch.dict(
                            systemd.__salt__,
                            {'config.get': self.mock_true,
                             'cmd.run_all': self.mock_run_all_success}):
                        ret = func(*args)
                        self.assertTrue(ret)
                        self.mock_run_all_success.assert_called_with(
                            ['systemd-run', '--scope'] + systemctl_command,
                            python_shell=False,
                            redirect_stderr=True)

                    # Scope enabled, failed
                    with patch.dict(
                            systemd.__salt__,
                            {'config.get': self.mock_true,
                             'cmd.run_all': self.mock_run_all_failure}):
                        self.assertRaises(
                            CommandExecutionError,
                            func, *args)
                        self.mock_run_all_failure.assert_called_with(
                            ['systemd-run', '--scope'] + systemctl_command,
                            python_shell=False,
                            redirect_stderr=True)

                    # Scope disabled, successful
                    with patch.dict(
                            systemd.__salt__,
                            {'config.get': self.mock_false,
                             'cmd.run_all': self.mock_run_all_success}):
                        ret = func(*args)
                        self.assertTrue(ret)
                        self.mock_run_all_success.assert_called_with(
                            systemctl_command,
                            python_shell=False,
                            redirect_stderr=True)

                    # Scope disabled, failed
                    with patch.dict(
                            systemd.__salt__,
                            {'config.get': self.mock_false,
                             'cmd.run_all': self.mock_run_all_failure}):
                        self.assertRaises(
                            CommandExecutionError,
                            func, *args)
                        self.mock_run_all_failure.assert_called_with(
                            systemctl_command,
                            python_shell=False,
                            redirect_stderr=True)

                # Does not have scopes available
                with patch.object(salt.utils.systemd, 'has_scope', self.mock_false):

                    # The results should be the same irrespective of
                    # whether or not scope is enabled, since scope is not
                    # available, so we repeat the below tests with it both
                    # enabled and disabled.
                    for scope_mock in (self.mock_true, self.mock_false):

                        # Successful
                        with patch.dict(
                                systemd.__salt__,
                                {'config.get': scope_mock,
                                 'cmd.run_all': self.mock_run_all_success}):
                            ret = func(*args)
                            self.assertTrue(ret)
                            self.mock_run_all_success.assert_called_with(
                                systemctl_command,
                                python_shell=False,
                                redirect_stderr=True)

                        # Failed
                        with patch.dict(
                                systemd.__salt__,
                                {'config.get': scope_mock,
                                 'cmd.run_all': self.mock_run_all_failure}):
                            self.assertRaises(
                                CommandExecutionError,
                                func, *args)
                            self.mock_run_all_failure.assert_called_with(
                                systemctl_command,
                                python_shell=False,
                                redirect_stderr=True)

    def test_start(self):
        self._change_state('start')

    def test_stop(self):
        self._change_state('stop')

    def test_restart(self):
        self._change_state('restart')

    def test_reload(self):
        self._change_state('reload_')

    def test_force_reload(self):
        self._change_state('force_reload')

    def test_enable(self):
        self._change_state('enable')

    def test_mask(self):
        self._mask_unmask('mask', False)

    def test_mask_runtime(self):
        self._mask_unmask('mask', True)

    def test_unmask(self):
        # Test already masked
        self._mask_unmask('unmask', False)
        # Test not masked (should take no action and return True). We don't
        # need to repeat this in test_unmask_runtime.
        with patch.object(systemd, '_check_for_unit_changes', self.mock_none):
            with patch.object(systemd, 'masked', self.mock_false):
                self.assertTrue(systemd.unmask(self.unit_name))

    def test_unmask_runtime(self):
        # Test already masked
        self._mask_unmask('unmask', True)


if __name__ == '__main__':
    from integration import run_tests
    run_tests(SystemdTestCase, needs_daemon=False)
