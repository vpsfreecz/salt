# -*- coding: utf-8 -*-
'''
Support for Alternatives system

:codeauthor: Radek Rada <radek.rada@gmail.com>
'''
from __future__ import absolute_import

# Import python libs
import os
import logging

# Import Salt libs
import salt.utils


__outputter__ = {
    'display': 'txt',
    'install': 'txt',
    'remove': 'txt',
}

log = logging.getLogger(__name__)

# Don't shadow built-in's.
__func_alias__ = {
    'set_': 'set'
}


def __virtual__():
    '''
    Only if alternatives dir is available
    '''
    if os.path.isdir('/etc/alternatives'):
        return True
    return False


def _get_cmd():
    '''
    Alteratives commands and differ across distributions
    '''
    if __grains__['os_family'] == 'RedHat':
        return 'alternatives'
    return 'update-alternatives'


def display(name):
    '''
    Display alternatives settings for defined command name

    CLI Example:

    .. code-block:: bash

        salt '*' alternatives.display editor
    '''
    cmd = [_get_cmd(), '--display', name]
    out = __salt__['cmd.run_all'](cmd, python_shell=False)
    if out['retcode'] > 0 and out['stderr'] != '':
        return out['stderr']
    return out['stdout']


def show_link(name):
    '''
    Display master link for the alternative

    .. versionadded:: 2015.8.13,2016.3.4,2016.11.0

    CLI Example:

    .. code-block:: bash

        salt '*' alternatives.show_link editor
    '''

    if __grains__['os_family'] == 'RedHat':
        path = '/var/lib/'
    elif __grains__['os_family'] == 'Suse':
        path = '/var/lib/rpm/'
    else:
        path = '/var/lib/dpkg/'

    path += 'alternatives/{0}'.format(name)

    try:
        with salt.utils.fopen(path, 'rb') as r_file:
            return r_file.readlines()[1].rstrip('\n')
    except OSError:
        log.error(
            'alternatives: {0} does not exist'.format(name)
        )
    except (IOError, IndexError) as exc:
        log.error(
            'alternatives: unable to get master link for {0}. '
            'Exception: {1}'.format(name, exc)
        )

    return False


def show_current(name):
    '''
    Display the current highest-priority alternative for a given alternatives
    link

    CLI Example:

    .. code-block:: bash

        salt '*' alternatives.show_current editor
    '''
    alt_link_path = '/etc/alternatives/{0}'.format(name)
    try:
        return os.readlink(alt_link_path)
    except OSError:
        log.error(
            'alternatives: path {0} does not exist'.format(alt_link_path)
        )
    return False


def check_exists(name, path):
    '''
    Check if the given path is an alternative for a name.

    .. versionadded:: 2015.8.4

    CLI Example:

    .. code-block:: bash

        salt '*' alternatives.check_exists name path
    '''
    cmd = [_get_cmd(), '--display', name]
    out = __salt__['cmd.run_all'](cmd, python_shell=False)

    if out['retcode'] > 0 and out['stderr'] != '':
        return False

    return path in out['stdout'].splitlines()


def check_installed(name, path):
    '''
    Check if the current highest-priority match for a given alternatives link
    is set to the desired path

    CLI Example:

    .. code-block:: bash

        salt '*' alternatives.check_installed name path
    '''
    return show_current(name) == path


def install(name, link, path, priority):
    '''
    Install symbolic links determining default commands

    CLI Example:

    .. code-block:: bash

        salt '*' alternatives.install editor /usr/bin/editor /usr/bin/emacs23 50
    '''
    cmd = [_get_cmd(), '--install', link, name, path, str(priority)]
    out = __salt__['cmd.run_all'](cmd, python_shell=False)
    if out['retcode'] > 0 and out['stderr'] != '':
        return out['stderr']
    return out['stdout']


def remove(name, path):
    '''
    Remove symbolic links determining the default commands.

    CLI Example:

    .. code-block:: bash

        salt '*' alternatives.remove name path
    '''
    cmd = [_get_cmd(), '--remove', name, path]
    out = __salt__['cmd.run_all'](cmd, python_shell=False)
    if out['retcode'] > 0:
        return out['stderr']
    return out['stdout']


def auto(name):
    '''
    Trigger alternatives to set the path for <name> as
    specified by priority.

    CLI Example:

    .. code-block:: bash

        salt '*' alternatives.auto name
    '''
    cmd = [_get_cmd(), '--auto', name]
    out = __salt__['cmd.run_all'](cmd, python_shell=False)
    if out['retcode'] > 0:
        return out['stderr']
    return out['stdout']


def set_(name, path):
    '''
    Manually set the alternative <path> for <name>.

    CLI Example:

    .. code-block:: bash

        salt '*' alternatives.set name path
    '''
    cmd = [_get_cmd(), '--set', name, path]
    out = __salt__['cmd.run_all'](cmd, python_shell=False)
    if out['retcode'] > 0:
        return out['stderr']
    return out['stdout']
