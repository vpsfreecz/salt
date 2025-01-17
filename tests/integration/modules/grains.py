# -*- coding: utf-8 -*-
'''
Test the grains module
'''

# Import python libs
from __future__ import absolute_import
import os
import time

# Import Salt Testing libs
from salttesting import skipIf
from salttesting.helpers import destructiveTest, ensure_in_syspath

ensure_in_syspath('../../')

# Import salt libs
import integration


class TestModulesGrains(integration.ModuleCase):
    '''
    Test the grains module
    '''
    def test_items(self):
        '''
        grains.items
        '''
        opts = self.minion_opts
        self.assertEqual(
            self.run_function('grains.items')['test_grain'],
            opts['grains']['test_grain']
        )

    def test_item(self):
        '''
        grains.item
        '''
        opts = self.minion_opts
        self.assertEqual(
            self.run_function('grains.item', ['test_grain'])['test_grain'],
            opts['grains']['test_grain']
        )

    def test_ls(self):
        '''
        grains.ls
        '''
        check_for = (
            'cpuarch',
            'cpu_flags',
            'cpu_model',
            'domain',
            'fqdn',
            'host',
            'kernel',
            'kernelrelease',
            'localhost',
            'mem_total',
            'num_cpus',
            'os',
            'os_family',
            'path',
            'ps',
            'pythonpath',
            'pythonversion',
            'saltpath',
            'saltversion',
            'virtual',
        )
        lsgrains = self.run_function('grains.ls')
        for grain_name in check_for:
            self.assertTrue(grain_name in lsgrains)

    @skipIf(os.environ.get('TRAVIS_PYTHON_VERSION', None) is not None,
            'Travis environment can\'t keep up with salt refresh')
    def test_set_val(self):
        '''
        test grains.set_val
        '''
        self.assertEqual(
                self.run_function(
                    'grains.setval',
                    ['setgrain', 'grainval']),
                {'setgrain': 'grainval'})
        time.sleep(5)
        ret = self.run_function('grains.item', ['setgrain'])
        if not ret:
            # Sleep longer, sometimes test systems get bogged down
            time.sleep(20)
            ret = self.run_function('grains.item', ['setgrain'])
        self.assertTrue(ret)

    def test_get(self):
        '''
        test grains.get
        '''
        self.assertEqual(
                self.run_function(
                    'grains.get',
                    ['level1:level2']),
                'foo')


class GrainsAppendTestCase(integration.ModuleCase):
    '''
    Tests written specifically for the grains.append function.
    '''
    GRAIN_KEY = 'salttesting-grain-key'
    GRAIN_VAL = 'my-grain-val'

    @destructiveTest
    def tearDown(self):
        test_grain = self.run_function('grains.get', [self.GRAIN_KEY])
        if test_grain and test_grain == [self.GRAIN_VAL]:
            self.run_function('grains.remove', [self.GRAIN_KEY, self.GRAIN_VAL])

    @destructiveTest
    def test_grains_append(self):
        '''
        Tests the return of a simple grains.append call.
        '''
        ret = self.run_function('grains.append', [self.GRAIN_KEY, self.GRAIN_VAL])
        self.assertEqual(ret[self.GRAIN_KEY], [self.GRAIN_VAL])

    @destructiveTest
    def test_grains_append_val_already_present(self):
        '''
        Tests the return of a grains.append call when the value is already present in the grains list.
        '''
        messaging = 'The val {0} was already in the list salttesting-grain-key'.format(self.GRAIN_VAL)

        # First, make sure the test grain is present
        self.run_function('grains.append', [self.GRAIN_KEY, self.GRAIN_VAL])

        # Now try to append again
        ret = self.run_function('grains.append', [self.GRAIN_KEY, self.GRAIN_VAL])
        self.assertEqual(messaging, ret)

    @destructiveTest
    def test_grains_append_val_is_list(self):
        '''
        Tests the return of a grains.append call when val is passed in as a list.
        '''
        second_grain = self.GRAIN_VAL + '-2'
        ret = self.run_function('grains.append', [self.GRAIN_KEY, [self.GRAIN_VAL, second_grain]])
        self.assertEqual(ret[self.GRAIN_KEY], [self.GRAIN_VAL, second_grain])

    @destructiveTest
    def test_grains_append_call_twice(self):
        '''
        Tests the return of a grains.append call when the value is already present
        but also ensure the grain is not listed twice.
        '''
        # First, add the test grain.
        self.run_function('grains.append', [self.GRAIN_KEY, self.GRAIN_VAL])

        # Call the function again, which results in a string message, as tested in
        # test_grains_append_val_already_present above.
        self.run_function('grains.append', [self.GRAIN_KEY, self.GRAIN_VAL])

        # Now make sure the grain doesn't show up twice.
        grains = self.run_function('grains.items')
        count = 0
        for grain in grains.keys():
            if grain == self.GRAIN_KEY:
                count += 1

        # We should only have hit the grain key once.
        self.assertEqual(count, 1)


if __name__ == '__main__':
    from integration import run_tests
    run_tests(TestModulesGrains)
