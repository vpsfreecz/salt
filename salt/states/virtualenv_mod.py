# -*- coding: utf-8 -*-
'''
Setup of Python virtualenv sandboxes.

.. versionadded:: 0.17.0
'''
from __future__ import absolute_import

# Import python libs
import logging
import os

# Import salt libs
import salt.version
import salt.utils

log = logging.getLogger(__name__)

# Define the module's virtual name
__virtualname__ = 'virtualenv'


def __virtual__():
    return __virtualname__


def managed(name,
            venv_bin=None,
            requirements=None,
            system_site_packages=False,
            distribute=False,
            use_wheel=False,
            clear=False,
            python=None,
            extra_search_dir=None,
            never_download=None,
            prompt=None,
            user=None,
            no_chown=False,
            cwd=None,
            index_url=None,
            extra_index_url=None,
            pre_releases=False,
            no_deps=False,
            pip_download=None,
            pip_download_cache=None,
            pip_exists_action=None,
            proxy=None,
            use_vt=False,
            env_vars=None,
            pip_upgrade=False,
            pip_pkgs=None):
    '''
    Create a virtualenv and optionally manage it with pip

    name
        Path to the virtualenv.

    requirements: None
        Path to a pip requirements file. If the path begins with ``salt://``
        the file will be transferred from the master file server.

    use_wheel: False
        Prefer wheel archives (requires pip >= 1.4).

    user: None
        The user under which to run virtualenv and pip.

    no_chown: False
        When user is given, do not attempt to copy and chown a requirements file
        (needed if the requirements file refers to other files via relative
        paths, as the copy-and-chown procedure does not account for such files)

    cwd: None
        Path to the working directory where `pip install` is executed.

    no_deps: False
        Pass `--no-deps` to `pip install`.

    pip_exists_action: None
        Default action of pip when a path already exists: (s)witch, (i)gnore,
        (w)ipe, (b)ackup.

    proxy: None
        Proxy address which is passed to `pip install`.

    env_vars: None
        Set environment variables that some builds will depend on. For example,
        a Python C-module may have a Makefile that needs INCLUDE_PATH set to
        pick up a header file while compiling.

    pip_upgrade: False
        Pass `--upgrade` to `pip install`.

    pip_pkgs: None
        As an alternative to `requirements`, pass a list of pip packages that
        should be installed.

    Also accepts any kwargs that the virtualenv module will. However, some
    kwargs, such as the ``pip`` option, require ``- distribute: True``.

    .. code-block:: yaml

        /var/www/myvirtualenv.com:
          virtualenv.managed:
            - system_site_packages: False
            - requirements: salt://REQUIREMENTS.txt
    '''
    ret = {'name': name, 'result': True, 'comment': '', 'changes': {}}

    if 'virtualenv.create' not in __salt__:
        ret['result'] = False
        ret['comment'] = 'Virtualenv was not detected on this system'
        return ret

    if salt.utils.is_windows():
        venv_py = os.path.join(name, 'Scripts', 'python.exe')
    else:
        venv_py = os.path.join(name, 'bin', 'python')
    venv_exists = os.path.exists(venv_py)

    # Bail out early if the specified requirements file can't be found
    if requirements and requirements.startswith('salt://'):
        cached_requirements = __salt__['cp.is_cached'](requirements, __env__)
        if not cached_requirements:
            # It's not cached, let's cache it.
            cached_requirements = __salt__['cp.cache_file'](
                requirements, __env__
            )
        # Check if the master version has changed.
        if __salt__['cp.hash_file'](requirements, __env__) != \
                __salt__['cp.hash_file'](cached_requirements, __env__):
            cached_requirements = __salt__['cp.cache_file'](
                requirements, __env__
            )
        if not cached_requirements:
            ret.update({
                'result': False,
                'comment': 'pip requirements file {0!r} not found'.format(
                    requirements
                )
            })
            return ret
        requirements = cached_requirements

    # If it already exists, grab the version for posterity
    if venv_exists and clear:
        ret['changes']['cleared_packages'] = \
            __salt__['pip.freeze'](bin_env=name)
        ret['changes']['old'] = \
            __salt__['cmd.run_stderr']('{0} -V'.format(venv_py)).strip('\n')

    # Create (or clear) the virtualenv
    if __opts__['test']:
        if venv_exists and clear:
            ret['result'] = None
            ret['comment'] = 'Virtualenv {0} is set to be cleared'.format(name)
            return ret
        if venv_exists and not clear:
            #ret['result'] = None
            ret['comment'] = 'Virtualenv {0} is already created'.format(name)
            return ret
        ret['result'] = None
        ret['comment'] = 'Virtualenv {0} is set to be created'.format(name)
        return ret

    if not venv_exists or (venv_exists and clear):
        _ret = __salt__['virtualenv.create'](
            name,
            venv_bin=venv_bin,
            system_site_packages=system_site_packages,
            distribute=distribute,
            clear=clear,
            python=python,
            extra_search_dir=extra_search_dir,
            never_download=never_download,
            prompt=prompt,
            user=user,
            use_vt=use_vt,
        )

        if _ret['retcode'] != 0:
            ret['result'] = False
            ret['comment'] = _ret['stdout'] + _ret['stderr']
            return ret

        ret['result'] = True
        ret['changes']['new'] = __salt__['cmd.run_stderr'](
            '{0} -V'.format(venv_py)).strip('\n')

        if clear:
            ret['comment'] = 'Cleared existing virtualenv'
        else:
            ret['comment'] = 'Created new virtualenv'

    elif venv_exists:
        ret['comment'] = 'virtualenv exists'

    if use_wheel:
        min_version = '1.4'
        cur_version = __salt__['pip.version'](bin_env=name)
        if not salt.utils.compare_versions(ver1=cur_version, oper='>=',
                                           ver2=min_version):
            ret['result'] = False
            ret['comment'] = ('The \'use_wheel\' option is only supported in '
                              'pip {0} and newer. The version of pip detected '
                              'was {1}.').format(min_version, cur_version)
            return ret

    # Populate the venv via a requirements file
    if requirements or pip_pkgs:
        before = set(__salt__['pip.freeze'](bin_env=name, user=user, use_vt=use_vt))
        _ret = __salt__['pip.install'](
            pkgs=pip_pkgs,
            requirements=requirements,
            bin_env=name,
            use_wheel=use_wheel,
            user=user,
            cwd=cwd,
            index_url=index_url,
            extra_index_url=extra_index_url,
            download=pip_download,
            download_cache=pip_download_cache,
            no_chown=no_chown,
            pre_releases=pre_releases,
            exists_action=pip_exists_action,
            upgrade=pip_upgrade,
            no_deps=no_deps,
            proxy=proxy,
            use_vt=use_vt,
            env_vars=env_vars
        )
        ret['result'] &= _ret['retcode'] == 0
        if _ret['retcode'] > 0:
            ret['comment'] = '{0}\n{1}\n{2}'.format(ret['comment'],
                                                    _ret['stdout'],
                                                    _ret['stderr'])

        after = set(__salt__['pip.freeze'](bin_env=name))

        new = list(after - before)
        old = list(before - after)

        if new or old:
            ret['changes']['packages'] = {
                'new': new if new else '',
                'old': old if old else ''}
    return ret

manage = salt.utils.alias_function(managed, 'manage')
