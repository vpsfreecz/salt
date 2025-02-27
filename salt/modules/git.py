# -*- coding: utf-8 -*-
'''
Support for the Git SCM
'''
from __future__ import absolute_import

# Import python libs
import copy
import logging
import os
import re
import shlex
from distutils.version import LooseVersion as _LooseVersion

# Import salt libs
import salt.utils
import salt.utils.files
import salt.utils.itertools
import salt.utils.url
from salt.exceptions import SaltInvocationError, CommandExecutionError
from salt.ext import six

log = logging.getLogger(__name__)

__func_alias__ = {
    'rm_': 'rm'
}


def __virtual__():
    '''
    Only load if git exists on the system
    '''
    return True if salt.utils.which('git') else False


def _check_worktree_support(failhard=True):
    '''
    Ensure that we don't try to operate on worktrees in git < 2.5.0.
    '''
    git_version = version(versioninfo=False)
    if _LooseVersion(git_version) < _LooseVersion('2.5.0'):
        if failhard:
            raise CommandExecutionError(
                'Worktrees are only supported in git 2.5.0 and newer '
                '(detected git version: ' + git_version + ')'
            )
        return False
    return True


def _config_getter(get_opt,
                   key,
                   value_regex=None,
                   cwd=None,
                   user=None,
                   ignore_retcode=False,
                   **kwargs):
    '''
    Common code for config.get_* functions, builds and runs the git CLI command
    and returns the result dict for the calling function to parse.
    '''
    kwargs = salt.utils.clean_kwargs(**kwargs)
    global_ = kwargs.pop('global', False)
    if kwargs:
        salt.utils.invalid_kwargs(kwargs)

    if cwd is None:
        if not global_:
            raise SaltInvocationError(
                '\'cwd\' argument required unless global=True'
            )
    else:
        cwd = _expand_path(cwd, user)

    if get_opt == '--get-regexp':
        if value_regex is not None \
                and not isinstance(value_regex, six.string_types):
            value_regex = str(value_regex)
    else:
        # Ignore value_regex
        value_regex = None

    command = ['git', 'config']
    command.extend(_which_git_config(global_, cwd, user))
    command.append(get_opt)
    command.append(key)
    if value_regex is not None:
        command.append(value_regex)
    return _git_run(command,
                    cwd=cwd,
                    runas=user,
                    ignore_retcode=ignore_retcode,
                    failhard=False)


def _expand_path(cwd, user):
    '''
    Expand home directory
    '''
    try:
        to_expand = '~' + user if user else '~'
    except TypeError:
        # Users should never be numeric but if we don't account for this then
        # we're going to get a traceback if someone passes this invalid input.
        to_expand = '~' + str(user) if user else '~'
    try:
        return os.path.join(os.path.expanduser(to_expand), cwd)
    except AttributeError:
        return os.path.join(os.path.expanduser(to_expand), str(cwd))


def _format_opts(opts):
    '''
    Common code to inspect opts and split them if necessary
    '''
    if opts is None:
        return []
    elif isinstance(opts, list):
        new_opts = []
        for item in opts:
            if isinstance(item, six.string_types):
                new_opts.append(item)
            else:
                new_opts.append(str(item))
        return new_opts
    else:
        if not isinstance(opts, six.string_types):
            opts = [str(opts)]
        else:
            opts = shlex.split(opts)
    try:
        if opts[-1] == '--':
            # Strip the '--' if it was passed at the end of the opts string,
            # it'll be added back (if necessary) in the calling function.
            # Putting this check here keeps it from having to be repeated every
            # time _format_opts() is invoked.
            return opts[:-1]
    except IndexError:
        pass
    return opts


def _git_run(command, cwd=None, runas=None, identity=None,
             ignore_retcode=False, failhard=True, redirect_stderr=False,
             **kwargs):
    '''
    simple, throw an exception with the error message on an error return code.

    this function may be moved to the command module, spliced with
    'cmd.run_all', and used as an alternative to 'cmd.run_all'. Some
    commands don't return proper retcodes, so this can't replace 'cmd.run_all'.
    '''
    env = {}

    if identity:
        _salt_cli = __opts__.get('__cli', '')
        errors = []
        missing_keys = []

        # if the statefile provides multiple identities, they need to be tried
        # (but also allow a string instead of a list)
        if not isinstance(identity, list):
            # force it into a list
            identity = [identity]

        # try each of the identities, independently
        for id_file in identity:
            if not os.path.isfile(id_file):
                missing_keys.append(id_file)
                log.warning('Identity file {0} does not exist'.format(id_file))
                continue

            env = {
                'GIT_IDENTITY': id_file
            }

            # copy wrapper to area accessible by ``runas`` user
            # currently no suppport in windows for wrapping git ssh
            ssh_id_wrapper = os.path.join(
                salt.utils.templates.TEMPLATE_DIRNAME,
                'git/ssh-id-wrapper'
            )
            if salt.utils.is_windows():
                for suffix in ('', ' (x86)'):
                    ssh_exe = (
                        'C:\\Program Files{0}\\Git\\bin\\ssh.exe'
                        .format(suffix)
                    )
                    if os.path.isfile(ssh_exe):
                        env['GIT_SSH_EXE'] = ssh_exe
                        break
                else:
                    raise CommandExecutionError(
                        'Failed to find ssh.exe, unable to use identity file'
                    )
                # Use the windows batch file instead of the bourne shell script
                ssh_id_wrapper += '.bat'
                env['GIT_SSH'] = ssh_id_wrapper
            else:
                tmp_file = salt.utils.mkstemp()
                salt.utils.files.copyfile(ssh_id_wrapper, tmp_file)
                os.chmod(tmp_file, 0o500)
                os.chown(tmp_file, __salt__['file.user_to_uid'](runas), -1)
                env['GIT_SSH'] = tmp_file

            if 'salt-call' not in _salt_cli \
                    and __salt__['ssh.key_is_encrypted'](id_file):
                errors.append(
                    'Identity file {0} is passphrase-protected and cannot be '
                    'used in a non-interactive command. Using salt-call from '
                    'the minion will allow a passphrase-protected key to be '
                    'used.'.format(id_file)
                )
                continue

            log.info(
                'Attempting git authentication using identity file {0}'
                .format(id_file)
            )

            try:
                result = __salt__['cmd.run_all'](
                    command,
                    cwd=cwd,
                    runas=runas,
                    env=env,
                    python_shell=False,
                    log_callback=salt.utils.url.redact_http_basic_auth,
                    ignore_retcode=ignore_retcode,
                    redirect_stderr=redirect_stderr,
                    **kwargs)
            finally:
                if not salt.utils.is_windows() and 'GIT_SSH' in env:
                    os.remove(env['GIT_SSH'])

            # If the command was successful, no need to try additional IDs
            if result['retcode'] == 0:
                return result
            else:
                err = result['stdout' if redirect_stderr else 'stderr']
                if err:
                    errors.append(salt.utils.url.redact_http_basic_auth(err))

        # We've tried all IDs and still haven't passed, so error out
        if failhard:
            msg = (
                'Unable to authenticate using identity file:\n\n{0}'.format(
                    '\n'.join(errors)
                )
            )
            if missing_keys:
                if errors:
                    msg += '\n\n'
                msg += (
                    'The following identity file(s) were not found: {0}'
                    .format(', '.join(missing_keys))
                )
            raise CommandExecutionError(msg)
        return result

    else:
        result = __salt__['cmd.run_all'](
            command,
            cwd=cwd,
            runas=runas,
            env=env,
            python_shell=False,
            log_callback=salt.utils.url.redact_http_basic_auth,
            ignore_retcode=ignore_retcode,
            redirect_stderr=redirect_stderr,
            **kwargs)

        if result['retcode'] == 0:
            return result
        else:
            if failhard:
                gitcommand = ' '.join(command) \
                    if isinstance(command, list) \
                    else command
                msg = 'Command \'{0}\' failed'.format(
                    salt.utils.url.redact_http_basic_auth(gitcommand)
                )
                err = result['stdout' if redirect_stderr else 'stderr']
                if err:
                    msg += ': {0}'.format(
                        salt.utils.url.redact_http_basic_auth(err)
                    )
                raise CommandExecutionError(msg)
            return result


def _get_toplevel(path, user=None):
    '''
    Use git rev-parse to return the top level of a repo
    '''
    return _git_run(
        ['git', 'rev-parse', '--show-toplevel'],
        cwd=path,
        runas=user
    )['stdout']


def _git_config(cwd, user):
    '''
    Helper to retrieve git config options
    '''
    contextkey = 'git.config.' + cwd
    if contextkey not in __context__:
        git_dir = rev_parse(cwd,
                            opts=['--git-dir'],
                            user=user,
                            ignore_retcode=True)
        if not os.path.isabs(git_dir):
            paths = (cwd, git_dir, 'config')
        else:
            paths = (git_dir, 'config')
        __context__[contextkey] = os.path.join(*paths)
    return __context__[contextkey]


def _which_git_config(global_, cwd, user):
    '''
    Based on whether global or local config is desired, return a list of CLI
    args to include in the git config command.
    '''
    if global_:
        return ['--global']
    version_ = _LooseVersion(version(versioninfo=False))
    if version_ >= _LooseVersion('1.7.10.2'):
        # --local added in 1.7.10.2
        return ['--local']
    else:
        # For earlier versions, need to specify the path to the git config file
        return ['--file', _git_config(cwd, user)]


def add(cwd, filename, opts='', user=None, ignore_retcode=False):
    '''
    .. versionchanged:: 2015.8.0
        The ``--verbose`` command line argument is now implied

    Interface to `git-add(1)`_

    cwd
        The path to the git checkout

    filename
        The location of the file/directory to add, relative to ``cwd``

    opts
        Any additional options to add to the command line, in a single string

        .. note::
            On the Salt CLI, if the opts are preceded with a dash, it is
            necessary to precede them with ``opts=`` (as in the CLI examples
            below) to avoid causing errors with Salt's own argument parsing.

    user
        User under which to run the git command. By default, the command is run
        by the user under which the minion is running.

    ignore_retcode : False
        If ``True``, do not log an error to the minion log if the git command
        returns a nonzero exit status.

        .. versionadded:: 2015.8.0

    .. _`git-add(1)`: http://git-scm.com/docs/git-add


    CLI Examples:

    .. code-block:: bash

        salt myminion git.add /path/to/repo foo/bar.py
        salt myminion git.add /path/to/repo foo/bar.py opts='--dry-run'
    '''
    cwd = _expand_path(cwd, user)
    if not isinstance(filename, six.string_types):
        filename = str(filename)
    command = ['git', 'add', '--verbose']
    command.extend(
        [x for x in _format_opts(opts) if x not in ('-v', '--verbose')]
    )
    command.extend(['--', filename])
    return _git_run(command,
                    cwd=cwd,
                    runas=user,
                    ignore_retcode=ignore_retcode)['stdout']


def archive(cwd,
            output,
            rev='HEAD',
            fmt=None,
            prefix=None,
            user=None,
            ignore_retcode=False,
            **kwargs):
    '''
    .. versionchanged:: 2015.8.0
        Returns ``True`` if successful, raises an error if not.

    Interface to `git-archive(1)`_, exports a tarball/zip file of the
    repository

    cwd
        The path to be archived

        .. note::
            ``git archive`` permits a partial archive to be created. Thus, this
            path does not need to be the root of the git repository. Only the
            files within the directory specified by ``cwd`` (and its
            subdirectories) will be in the resulting archive. For example, if
            there is a git checkout at ``/tmp/foo``, then passing
            ``/tmp/foo/bar`` as the ``cwd`` will result in just the files
            underneath ``/tmp/foo/bar`` to be exported as an archive.

    output
        The path of the archive to be created

    overwrite : False
        Unless set to ``True``, Salt will over overwrite an existing archive at
        the path specified by the ``output`` argument.

        .. versionadded:: 2015.8.0

    rev : HEAD
        The revision from which to create the archive

    format
        Manually specify the file format of the resulting archive. This
        argument can be omitted, and ``git archive`` will attempt to guess the
        archive type (and compression) from the filename. ``zip``, ``tar``,
        ``tar.gz``, and ``tgz`` are extensions that are recognized
        automatically, and git can be configured to support other archive types
        with the addition of git configuration keys.

        See the `git-archive(1)`_ manpage explanation of the
        ``--format`` argument (as well as the ``CONFIGURATION`` section of the
        manpage) for further information.

        .. versionadded:: 2015.8.0

    fmt
        Replaced by ``format`` in version 2015.8.0

        .. deprecated:: 2015.8.0

    prefix
        Prepend ``<prefix>`` to every filename in the archive. If unspecified,
        the name of the directory at the top level of the repository will be
        used as the prefix (e.g. if ``cwd`` is set to ``/foo/bar/baz``, the
        prefix will be ``baz``, and the resulting archive will contain a
        top-level directory by that name).

        .. note::
            The default behavior if the ``--prefix`` option for ``git archive``
            is not specified is to not prepend a prefix, so Salt's behavior
            differs slightly from ``git archive`` in this respect. Use
            ``prefix=''`` to create an archive with no prefix.

        .. versionchanged:: 2015.8.0
            The behavior of this argument has been changed slightly. As of
            this version, it is necessary to include the trailing slash when
            specifying a prefix, if the prefix is intended to create a
            top-level directory.

    user
        User under which to run the git command. By default, the command is run
        by the user under which the minion is running.

    ignore_retcode : False
        If ``True``, do not log an error to the minion log if the git command
        returns a nonzero exit status.

        .. versionadded:: 2015.8.0

    .. _`git-archive(1)`: http://git-scm.com/docs/git-archive


    CLI Example:

    .. code-block:: bash

        salt myminion git.archive /path/to/repo /path/to/archive.tar
    '''
    cwd = _expand_path(cwd, user)
    output = _expand_path(output, user)
    # Sanitize kwargs and make sure that no invalid ones were passed. This
    # allows us to accept 'format' as an argument to this function without
    # shadowing the format() global, while also not allowing unwanted arguments
    # to be passed.
    kwargs = salt.utils.clean_kwargs(**kwargs)
    format_ = kwargs.pop('format', None)
    if kwargs:
        salt.utils.invalid_kwargs(kwargs)

    if fmt:
        salt.utils.warn_until(
            'Nitrogen',
            'The \'fmt\' argument to git.archive has been deprecated, please '
            'use \'format\' instead.'
        )
        format_ = fmt

    command = ['git', 'archive']
    # If prefix was set to '' then we skip adding the --prefix option
    if prefix != '':
        if prefix:
            if not isinstance(prefix, six.string_types):
                prefix = str(prefix)
        else:
            prefix = os.path.basename(cwd) + '/'
        command.extend(['--prefix', prefix])

    if format_:
        if not isinstance(format_, six.string_types):
            format_ = str(format_)
        command.extend(['--format', format_])
    command.extend(['--output', output, rev])
    _git_run(command, cwd=cwd, runas=user, ignore_retcode=ignore_retcode)
    # No output (unless --verbose is used, and we don't want all files listed
    # in the output in case there are thousands), so just return True
    return True


def branch(cwd, name=None, opts='', user=None, ignore_retcode=False):
    '''
    Interface to `git-branch(1)`_

    cwd
        The path to the git checkout

    name
        Name of the branch on which to operate. If not specified, the current
        branch will be assumed.

    opts
        Any additional options to add to the command line, in a single string

        .. note::
            To create a branch based on something other than HEAD, pass the
            name of the revision as ``opts``. If the revision is in the format
            ``remotename/branch``, then this will also set the remote tracking
            branch.

            Additionally, on the Salt CLI, if the opts are preceded with a
            dash, it is necessary to precede them with ``opts=`` (as in the CLI
            examples below) to avoid causing errors with Salt's own argument
            parsing.

    user
        User under which to run the git command. By default, the command is run
        by the user under which the minion is running.

    ignore_retcode : False
        If ``True``, do not log an error to the minion log if the git command
        returns a nonzero exit status.

        .. versionadded:: 2015.8.0

    .. _`git-branch(1)`: http://git-scm.com/docs/git-branch


    CLI Examples:

    .. code-block:: bash

        # Set remote tracking branch
        salt myminion git.branch /path/to/repo mybranch opts='--set-upstream-to origin/mybranch'
        # Create new branch
        salt myminion git.branch /path/to/repo mybranch upstream/somebranch
        # Delete branch
        salt myminion git.branch /path/to/repo mybranch opts='-d'
        # Rename branch (2015.8.0 and later)
        salt myminion git.branch /path/to/repo newbranch opts='-m oldbranch'
    '''
    cwd = _expand_path(cwd, user)
    command = ['git', 'branch']
    command.extend(_format_opts(opts))
    if name is not None:
        command.append(name)
    _git_run(command, cwd=cwd, runas=user, ignore_retcode=ignore_retcode)
    return True


def checkout(cwd,
             rev=None,
             force=False,
             opts='',
             user=None,
             ignore_retcode=False):
    '''
    Interface to `git-checkout(1)`_

    cwd
        The path to the git checkout

    opts
        Any additional options to add to the command line, in a single string

        .. note::
            On the Salt CLI, if the opts are preceded with a dash, it is
            necessary to precede them with ``opts=`` (as in the CLI examples
            below) to avoid causing errors with Salt's own argument parsing.

    rev
        The remote branch or revision to checkout.

        .. versionchanged:: 2015.8.0
            Optional when using ``-b`` or ``-B`` in ``opts``.

    force : False
        Force a checkout even if there might be overwritten changes

    user
        User under which to run the git command. By default, the command is run
        by the user under which the minion is running.

    ignore_retcode : False
        If ``True``, do not log an error to the minion log if the git command
        returns a nonzero exit status.

        .. versionadded:: 2015.8.0

    .. _`git-checkout(1)`: http://git-scm.com/docs/git-checkout


    CLI Examples:

    .. code-block:: bash

        # Checking out local local revisions
        salt myminion git.checkout /path/to/repo somebranch user=jeff
        salt myminion git.checkout /path/to/repo opts='testbranch -- conf/file1 file2'
        salt myminion git.checkout /path/to/repo rev=origin/mybranch opts='--track'
        # Checking out remote revision into new branch
        salt myminion git.checkout /path/to/repo upstream/master opts='-b newbranch'
        # Checking out current revision into new branch (2015.8.0 and later)
        salt myminion git.checkout /path/to/repo opts='-b newbranch'
    '''
    cwd = _expand_path(cwd, user)
    command = ['git', 'checkout']
    if force:
        command.append('--force')
    opts = _format_opts(opts)
    command.extend(opts)
    checkout_branch = any(x in opts for x in ('-b', '-B'))
    if rev is None:
        if not checkout_branch:
            raise SaltInvocationError(
                '\'rev\' argument is required unless -b or -B in opts'
            )
    else:
        if not isinstance(rev, six.string_types):
            rev = str(rev)
        command.append(rev)
    # Checkout message goes to stderr
    return _git_run(command,
                    cwd=cwd,
                    runas=user,
                    ignore_retcode=ignore_retcode,
                    redirect_stderr=True)['stdout']


def clone(cwd,
          url=None,  # Remove default value once 'repository' arg is removed
          name=None,
          opts='',
          user=None,
          identity=None,
          https_user=None,
          https_pass=None,
          ignore_retcode=False,
          repository=None):
    '''
    Interface to `git-clone(1)`_

    cwd
        Location of git clone

        .. versionchanged:: 2015.8.0
            If ``name`` is passed, then the clone will be made *within* this
            directory.

    url
        The URL of the repository to be cloned

        .. versionchanged:: 2015.8.0
            Argument renamed from ``repository`` to ``url``

    name
        Optional alternate name for the top-level directory to be created by
        the clone

        .. versionadded:: 2015.8.0

    opts
        Any additional options to add to the command line, in a single string

    user
        User under which to run the git command. By default, the command is run
        by the user under which the minion is running.

        Run git as a user other than what the minion runs as

    identity
        Path to a private key to use for ssh URLs

        .. warning::

            Unless Salt is invoked from the minion using ``salt-call``, the
            key(s) must be passphraseless. For greater security with
            passphraseless private keys, see the `sshd(8)`_ manpage for
            information on securing the keypair from the remote side in the
            ``authorized_keys`` file.

            .. _`sshd(8)`: http://www.man7.org/linux/man-pages/man8/sshd.8.html#AUTHORIZED_KEYS_FILE%20FORMAT

        .. versionchanged:: 2015.8.7
            Salt will no longer attempt to use passphrase-protected keys unless
            invoked from the minion using ``salt-call``, to prevent blocking
            waiting for user input.

    https_user
        Set HTTP Basic Auth username. Only accepted for HTTPS URLs.

        .. versionadded:: 20515.5.0

    https_pass
        Set HTTP Basic Auth password. Only accepted for HTTPS URLs.

        .. versionadded:: 2015.5.0

    ignore_retcode : False
        If ``True``, do not log an error to the minion log if the git command
        returns a nonzero exit status.

        .. versionadded:: 2015.8.0

    .. _`git-clone(1)`: http://git-scm.com/docs/git-clone

    CLI Example:

    .. code-block:: bash

        salt myminion git.clone /path/to/repo_parent_dir git://github.com/saltstack/salt.git
    '''
    cwd = _expand_path(cwd, user)
    if repository is not None:
        salt.utils.warn_until(
            'Nitrogen',
            'The \'repository\' argument to git.clone has been '
            'deprecated, please use \'url\' instead.'
        )
        url = repository

    if not url:
        raise SaltInvocationError('Missing \'url\' argument')

    try:
        url = salt.utils.url.add_http_basic_auth(url,
                                                 https_user,
                                                 https_pass,
                                                 https_only=True)
    except ValueError as exc:
        raise SaltInvocationError(exc.__str__())

    command = ['git', 'clone']
    command.extend(_format_opts(opts))
    command.extend(['--', url])
    if name is not None:
        if not isinstance(name, six.string_types):
            name = str(name)
        command.append(name)
        if not os.path.exists(cwd):
            os.makedirs(cwd)
        clone_cwd = cwd
    else:
        command.append(cwd)
        # Use '/tmp' instead of $HOME (/root for root user) to work around
        # upstream git bug. See the following comment on the Salt bug tracker
        # for more info:
        # https://github.com/saltstack/salt/issues/15519#issuecomment-128531310
        # On Windows, just fall back to None (runs git clone command using the
        # home directory as the cwd).
        clone_cwd = '/tmp' if not salt.utils.is_windows() else None
    _git_run(command,
             cwd=clone_cwd,
             runas=user,
             identity=identity,
             ignore_retcode=ignore_retcode)
    return True


def commit(cwd,
           message,
           opts='',
           user=None,
           filename=None,
           ignore_retcode=False):
    '''
    Interface to `git-commit(1)`_

    cwd
        The path to the git checkout

    message
        Commit message

    opts
        Any additional options to add to the command line, in a single string

        .. note::
            On the Salt CLI, if the opts are preceded with a dash, it is
            necessary to precede them with ``opts=`` (as in the CLI examples
            below) to avoid causing errors with Salt's own argument parsing.

            The ``-m`` option should not be passed here, as the commit message
            will be defined by the ``message`` argument.

    user
        User under which to run the git command. By default, the command is run
        by the user under which the minion is running.

    filename
        The location of the file/directory to commit, relative to ``cwd``.
        This argument is optional, and can be used to commit a file without
        first staging it.

        .. note::
            This argument only works on files which are already tracked by the
            git repository.

        .. versionadded:: 2015.8.0

    ignore_retcode : False
        If ``True``, do not log an error to the minion log if the git command
        returns a nonzero exit status.

        .. versionadded:: 2015.8.0

    .. _`git-commit(1)`: http://git-scm.com/docs/git-commit


    CLI Examples:

    .. code-block:: bash

        salt myminion git.commit /path/to/repo 'The commit message'
        salt myminion git.commit /path/to/repo 'The commit message' filename=foo/bar.py
    '''
    cwd = _expand_path(cwd, user)
    command = ['git', 'commit', '-m', message]
    command.extend(_format_opts(opts))
    if filename:
        if not isinstance(filename, six.string_types):
            filename = str(filename)
        # Add the '--' to terminate CLI args, but only if it wasn't already
        # passed in opts string.
        command.extend(['--', filename])
    return _git_run(command,
                    cwd=cwd,
                    runas=user,
                    ignore_retcode=ignore_retcode)['stdout']


def config_get(key,
               cwd=None,
               user=None,
               ignore_retcode=False,
               **kwargs):
    '''
    Get the value of a key in the git configuration file

    key
        The name of the configuration key to get

        .. versionchanged:: 2015.8.0
            Argument renamed from ``setting_name`` to ``key``

    cwd
        The path to the git checkout

        .. versionchanged:: 2015.8.0
            Now optional if ``global`` is set to ``True``

    global : False
        If ``True``, query the global git configuraton. Otherwise, only the
        local git configuration will be queried.

        .. versionadded:: 2015.8.0

    all : False
        If ``True``, return a list of all values set for ``key``. If the key
        does not exist, ``None`` will be returned.

        .. versionadded:: 2015.8.0

    user
        User under which to run the git command. By default, the command is run
        by the user under which the minion is running.

    ignore_retcode : False
        If ``True``, do not log an error to the minion log if the git command
        returns a nonzero exit status.

        .. versionadded:: 2015.8.0


    CLI Examples:

    .. code-block:: bash

        salt myminion git.config_get user.name cwd=/path/to/repo
        salt myminion git.config_get user.email global=True
        salt myminion git.config_get core.gitproxy cwd=/path/to/repo all=True
    '''
    # Sanitize kwargs and make sure that no invalid ones were passed. This
    # allows us to accept 'all' as an argument to this function without
    # shadowing all(), while also not allowing unwanted arguments to be passed.
    all_ = kwargs.pop('all', False)

    result = _config_getter('--get-all',
                            key,
                            cwd=cwd,
                            user=user,
                            ignore_retcode=ignore_retcode,
                            **kwargs)

    # git config --get exits with retcode of 1 when key does not exist
    if result['retcode'] == 1:
        return None
    ret = result['stdout'].splitlines()
    if all_:
        return ret
    else:
        try:
            return ret[-1]
        except IndexError:
            # Should never happen but I'm paranoid and don't like tracebacks
            return ''


def config_get_regexp(key,
                      value_regex=None,
                      cwd=None,
                      user=None,
                      ignore_retcode=False,
                      **kwargs):
    r'''
    .. versionadded:: 2015.8.0

    Get the value of a key or keys in the git configuration file using regexes
    for more flexible matching. The return data is a dictionary mapping keys to
    lists of values matching the ``value_regex``. If no values match, an empty
    dictionary will be returned.

    key
        Regex on which key names will be matched

    value_regex
        If specified, return all values matching this regex. The return data
        will be a dictionary mapping keys to lists of values matching the
        regex.

        .. important::
            Only values matching the ``value_regex`` will be part of the return
            data. So, if ``key`` matches a multivar, then it is possible that
            not all of the values will be returned. To get all values set for a
            multivar, simply omit the ``value_regex`` argument.

    cwd
        The path to the git checkout

    global : False
        If ``True``, query the global git configuraton. Otherwise, only the
        local git configuration will be queried.

    user
        User under which to run the git command. By default, the command is run
        by the user under which the minion is running.

    ignore_retcode : False
        If ``True``, do not log an error to the minion log if the git command
        returns a nonzero exit status.


    CLI Examples:

    .. code-block:: bash

        # Matches any values for key 'foo.bar'
        salt myminion git.config_get_regexp /path/to/repo foo.bar
        # Matches any value starting with 'baz' set for key 'foo.bar'
        salt myminion git.config_get_regexp /path/to/repo foo.bar 'baz.*'
        # Matches any key starting with 'user.'
        salt myminion git.config_get_regexp '^user\.' global=True
    '''
    result = _config_getter('--get-regexp',
                            key,
                            value_regex=value_regex,
                            cwd=cwd,
                            user=user,
                            ignore_retcode=ignore_retcode,
                            **kwargs)

    # git config --get exits with retcode of 1 when key does not exist
    ret = {}
    if result['retcode'] == 1:
        return ret
    for line in result['stdout'].splitlines():
        try:
            param, value = line.split(None, 1)
        except ValueError:
            continue
        ret.setdefault(param, []).append(value)
    return ret

config_get_regex = salt.utils.alias_function(config_get_regexp, 'config_get_regex')


def config_set(key,
               value=None,
               multivar=None,
               cwd=None,
               user=None,
               ignore_retcode=False,
               **kwargs):
    '''
    .. versionchanged:: 2015.8.0
        Return the value(s) of the key being set

    Set a key in the git configuration file

    cwd
        The path to the git checkout. Must be an absolute path, or the word
        ``global`` to indicate that a global key should be set.

        .. versionchanged:: 2014.7.0
            Made ``cwd`` argument optional if ``is_global=True``

    key
        The name of the configuration key to set

        .. versionchanged:: 2015.8.0
            Argument renamed from ``setting_name`` to ``key``

    value
        The value to set for the specified key. Incompatible with the
        ``multivar`` argument.

        .. versionchanged:: 2015.8.0
            Argument renamed from ``setting_value`` to ``value``

    add : False
        Add a value to a key, creating/updating a multivar

        .. versionadded:: 2015.8.0

    multivar
        Set a multivar all at once. Values can be comma-separated or passed as
        a Python list. Incompatible with the ``value`` argument.

        .. versionadded:: 2015.8.0

    user
        User under which to run the git command. By default, the command is run
        by the user under which the minion is running.

    ignore_retcode : False
        If ``True``, do not log an error to the minion log if the git command
        returns a nonzero exit status.

        .. versionadded:: 2015.8.0

    global : False
        If ``True``, set a global variable

    is_global : False
        If ``True``, set a global variable

        .. deprecated:: 2015.8.0
            Use ``global`` instead


    CLI Example:

    .. code-block:: bash

        salt myminion git.config_set user.email me@example.com cwd=/path/to/repo
        salt myminion git.config_set user.email foo@bar.com global=True
    '''
    kwargs = salt.utils.clean_kwargs(**kwargs)
    add_ = kwargs.pop('add', False)
    global_ = kwargs.pop('global', False)
    is_global = kwargs.pop('is_global', False)
    if kwargs:
        salt.utils.invalid_kwargs(kwargs)

    if is_global:
        salt.utils.warn_until(
            'Nitrogen',
            'The \'is_global\' argument to git.config_set has been '
            'deprecated, please set the \'cwd\' argument to \'global\' '
            'instead.'
        )
        global_ = True

    if cwd is None:
        if not global_:
            raise SaltInvocationError(
                '\'cwd\' argument required unless global=True'
            )
    else:
        cwd = _expand_path(cwd, user)

    if all(x is not None for x in (value, multivar)):
        raise SaltInvocationError(
            'Only one of \'value\' and \'multivar\' is permitted'
        )

    if value is not None:
        if not isinstance(value, six.string_types):
            value = str(value)
    if multivar is not None:
        if not isinstance(multivar, list):
            try:
                multivar = multivar.split(',')
            except AttributeError:
                multivar = str(multivar).split(',')
        else:
            new_multivar = []
            for item in multivar:
                if isinstance(item, six.string_types):
                    new_multivar.append(item)
                else:
                    new_multivar.append(str(item))
            multivar = new_multivar

    command_prefix = ['git', 'config']
    if global_:
        command_prefix.append('--global')

    if value is not None:
        command = copy.copy(command_prefix)
        if add_:
            command.append('--add')
        else:
            command.append('--replace-all')
        command.extend([key, value])
        _git_run(command,
                 cwd=cwd,
                 runas=user,
                 ignore_retcode=ignore_retcode)
    else:
        for idx, target in enumerate(multivar):
            command = copy.copy(command_prefix)
            if idx == 0:
                command.append('--replace-all')
            else:
                command.append('--add')
            command.extend([key, target])
            _git_run(command,
                     cwd=cwd,
                     runas=user,
                     ignore_retcode=ignore_retcode)
    return config_get(key,
                      user=user,
                      cwd=cwd,
                      ignore_retcode=ignore_retcode,
                      **{'all': True, 'global': global_})


def config_unset(key,
                 value_regex=None,
                 cwd=None,
                 user=None,
                 ignore_retcode=False,
                 **kwargs):
    '''
    .. versionadded:: 2015.8.0

    Unset a key in the git configuration file

    cwd
        The path to the git checkout. Must be an absolute path, or the word
        ``global`` to indicate that a global key should be unset.

    key
        The name of the configuration key to unset

    value_regex
        Regular expression that matches exactly one key, used to delete a
        single value from a multivar. Ignored if ``all`` is set to ``True``.

    all : False
        If ``True`` unset all values for a multivar. If ``False``, and ``key``
        is a multivar, an error will be raised.

    global : False
        If ``True``, unset set a global variable. Otherwise, a local variable
        will be unset.

    user
        User under which to run the git command. By default, the command is run
        by the user under which the minion is running.

    ignore_retcode : False
        If ``True``, do not log an error to the minion log if the git command
        returns a nonzero exit status.


    CLI Example:

    .. code-block:: bash

        salt myminion git.config_unset /path/to/repo foo.bar
        salt myminion git.config_unset /path/to/repo foo.bar all=True
    '''
    kwargs = salt.utils.clean_kwargs(**kwargs)
    all_ = kwargs.pop('all', False)
    global_ = kwargs.pop('global', False)
    if kwargs:
        salt.utils.invalid_kwargs(kwargs)

    if cwd is None:
        if not global_:
            raise SaltInvocationError(
                '\'cwd\' argument required unless global=True'
            )
    else:
        cwd = _expand_path(cwd, user)

    command = ['git', 'config']
    if all_:
        command.append('--unset-all')
    else:
        command.append('--unset')
    command.extend(_which_git_config(global_, cwd, user))

    if not isinstance(key, six.string_types):
        key = str(key)
    command.append(key)
    if value_regex is not None:
        if not isinstance(value_regex, six.string_types):
            value_regex = str(value_regex)
        command.append(value_regex)
    ret = _git_run(command,
                   cwd=cwd if cwd != 'global' else None,
                   runas=user,
                   ignore_retcode=ignore_retcode,
                   failhard=False)
    retcode = ret['retcode']
    if retcode == 0:
        return True
    elif retcode == 1:
        raise CommandExecutionError('Section or key is invalid')
    elif retcode == 5:
        if config_get(cwd,
                      key,
                      user=user,
                      ignore_retcode=ignore_retcode) is None:
            raise CommandExecutionError(
                'Key \'{0}\' does not exist'.format(key)
            )
        else:
            msg = 'Multiple values exist for key \'{0}\''.format(key)
            if value_regex is not None:
                msg += ' and value_regex matches multiple values'
            raise CommandExecutionError(msg)
    elif retcode == 6:
        raise CommandExecutionError('The value_regex is invalid')
    else:
        msg = (
            'Failed to unset key \'{0}\', git config returned exit code {1}'
            .format(key, retcode)
        )
        if ret['stderr']:
            msg += '; ' + ret['stderr']
        raise CommandExecutionError(msg)


def current_branch(cwd, user=None, ignore_retcode=False):
    '''
    Returns the current branch name of a local checkout. If HEAD is detached,
    return the SHA1 of the revision which is currently checked out.

    cwd
        The path to the git checkout

    user
        User under which to run the git command. By default, the command is run
        by the user under which the minion is running.

    ignore_retcode : False
        If ``True``, do not log an error to the minion log if the git command
        returns a nonzero exit status.

        .. versionadded:: 2015.8.0


    CLI Example:

    .. code-block:: bash

        salt myminion git.current_branch /path/to/repo
    '''
    cwd = _expand_path(cwd, user)
    command = ['git', 'rev-parse', '--abbrev-ref', 'HEAD']
    return _git_run(command,
                    cwd=cwd,
                    runas=user,
                    ignore_retcode=ignore_retcode)['stdout']


def describe(cwd, rev='HEAD', user=None, ignore_retcode=False):
    '''
    Returns the `git-describe(1)`_ string (or the SHA1 hash if there are no
    tags) for the given revision.

    cwd
        The path to the git checkout

    rev : HEAD
        The revision to describe

    user
        User under which to run the git command. By default, the command is run
        by the user under which the minion is running.

    ignore_retcode : False
        If ``True``, do not log an error to the minion log if the git command
        returns a nonzero exit status.

        .. versionadded:: 2015.8.0

    .. _`git-describe(1)`: http://git-scm.com/docs/git-describe


    CLI Examples:

    .. code-block:: bash

        salt myminion git.describe /path/to/repo
        salt myminion git.describe /path/to/repo develop
    '''
    cwd = _expand_path(cwd, user)
    if not isinstance(rev, six.string_types):
        rev = str(rev)
    command = ['git', 'describe']
    if _LooseVersion(version(versioninfo=False)) >= _LooseVersion('1.5.6'):
        command.append('--always')
    command.append(rev)
    return _git_run(command,
                    cwd=cwd,
                    runas=user,
                    ignore_retcode=ignore_retcode)['stdout']


def diff(cwd,
         item1=None,
         item2=None,
         opts='',
         user=None,
         no_index=False,
         cached=False,
         paths=None):
    '''
    .. versionadded:: 2015.8.12,2016.3.3,2016.11.0

    Interface to `git-diff(1)`_

    cwd
        The path to the git checkout

    item1 and item2
        Revision(s) to pass to the ``git diff`` command. One or both of these
        arguments may be ignored if some of the options below are set to
        ``True``. When ``cached`` is ``False``, and no revisions are passed
        to this function, then the current working tree will be compared
        against the index (i.e. unstaged changes). When two revisions are
        passed, they will be compared to each other.

    opts
        Any additional options to add to the command line, in a single string

        .. note::
            On the Salt CLI, if the opts are preceded with a dash, it is
            necessary to precede them with ``opts=`` (as in the CLI examples
            below) to avoid causing errors with Salt's own argument parsing.

    user
        User under which to run the git command. By default, the command is run
        by the user under which the minion is running.

    no_index : False
        When it is necessary to diff two files in the same repo against each
        other, and not diff two different revisions, set this option to
        ``True``. If this is left ``False`` in these instances, then a normal
        ``git diff`` will be performed against the index (i.e. unstaged
        changes), and files in the ``paths`` option will be used to narrow down
        the diff output.

        .. note::
            Requires Git 1.5.1 or newer. Additionally, when set to ``True``,
            ``item1`` and ``item2`` will be ignored.

    cached : False
        If ``True``, compare staged changes to ``item1`` (if specified),
        otherwise compare them to the most recent commit.

        .. note::
            ``item2`` is ignored if this option is is set to ``True``.

    paths
        File paths to pass to the ``git diff`` command. Can be passed as a
        comma-separated list or a Python list.

    .. _`git-diff(1)`: http://git-scm.com/docs/git-diff


    CLI Example:

    .. code-block:: bash

        # Perform diff against the index (staging area for next commit)
        salt myminion git.diff /path/to/repo
        # Compare staged changes to the most recent commit
        salt myminion git.diff /path/to/repo cached=True
        # Compare staged changes to a specific revision
        salt myminion git.diff /path/to/repo mybranch cached=True
        # Perform diff against the most recent commit (includes staged changes)
        salt myminion git.diff /path/to/repo HEAD
        # Diff two commits
        salt myminion git.diff /path/to/repo abcdef1 aabbccd
        # Diff two commits, only showing differences in the specified paths
        salt myminion git.diff /path/to/repo abcdef1 aabbccd paths=path/to/file1,path/to/file2
        # Diff two files with one being outside the working tree
        salt myminion git.diff /path/to/repo no_index=True paths=path/to/file1,/absolute/path/to/file2
    '''
    if no_index and cached:
        raise CommandExecutionError(
            'The \'no_index\' and \'cached\' options cannot be used together'
        )

    command = ['git', 'diff']
    command.extend(_format_opts(opts))

    if paths is not None and not isinstance(paths, (list, tuple)):
        try:
            paths = paths.split(',')
        except AttributeError:
            paths = str(paths).split(',')

    ignore_retcode = False
    failhard = True

    if no_index:
        if _LooseVersion(version(versioninfo=False)) < _LooseVersion('1.5.1'):
            raise CommandExecutionError(
                'The \'no_index\' option is only supported in Git 1.5.1 and '
                'newer'
            )
        ignore_retcode = True
        failhard = False
        command.append('--no-index')
        for value in [x for x in (item1, item2) if x]:
            log.warning(
                'Revision \'%s\' ignored in git diff, as revisions cannot be '
                'used when no_index=True', value
            )

    elif cached:
        command.append('--cached')
        if item1:
            command.append(item1)
        if item2:
            log.warning(
                'Second revision \'%s\' ignored in git diff, at most one '
                'revision is considered when cached=True', item2
            )

    else:
        for value in [x for x in (item1, item2) if x]:
            command.append(value)

    if paths:
        command.append('--')
        command.extend(paths)

    return _git_run(command,
                    cwd=cwd,
                    runas=user,
                    ignore_retcode=ignore_retcode,
                    failhard=failhard,
                    redirect_stderr=True)['stdout']


def fetch(cwd,
          remote=None,
          force=False,
          refspecs=None,
          opts='',
          user=None,
          identity=None,
          ignore_retcode=False):
    '''
    .. versionchanged:: 2015.8.2
        Return data is now a dictionary containing information on branches and
        tags that were added/updated

    Interface to `git-fetch(1)`_

    cwd
        The path to the git checkout

    remote
        Optional remote name to fetch. If not passed, then git will use its
        default behavior (as detailed in `git-fetch(1)`_).

        .. versionadded:: 2015.8.0

    force
        Force the fetch even when it is not a fast-forward.

        .. versionadded:: 2015.8.0

    refspecs
        Override the refspec(s) configured for the remote with this argument.
        Multiple refspecs can be passed, comma-separated.

        .. versionadded:: 2015.8.0

    opts
        Any additional options to add to the command line, in a single string

        .. note::
            On the Salt CLI, if the opts are preceded with a dash, it is
            necessary to precede them with ``opts=`` (as in the CLI examples
            below) to avoid causing errors with Salt's own argument parsing.

    user
        User under which to run the git command. By default, the command is run
        by the user under which the minion is running.

    identity
        Path to a private key to use for ssh URLs

        .. warning::

            Unless Salt is invoked from the minion using ``salt-call``, the
            key(s) must be passphraseless. For greater security with
            passphraseless private keys, see the `sshd(8)`_ manpage for
            information on securing the keypair from the remote side in the
            ``authorized_keys`` file.

            .. _`sshd(8)`: http://www.man7.org/linux/man-pages/man8/sshd.8.html#AUTHORIZED_KEYS_FILE%20FORMAT

        .. versionchanged:: 2015.8.7
            Salt will no longer attempt to use passphrase-protected keys unless
            invoked from the minion using ``salt-call``, to prevent blocking
            waiting for user input.

    ignore_retcode : False
        If ``True``, do not log an error to the minion log if the git command
        returns a nonzero exit status.

        .. versionadded:: 2015.8.0

    .. _`git-fetch(1)`: http://git-scm.com/docs/git-fetch


    CLI Example:

    .. code-block:: bash

        salt myminion git.fetch /path/to/repo upstream
        salt myminion git.fetch /path/to/repo identity=/root/.ssh/id_rsa
    '''
    cwd = _expand_path(cwd, user)
    command = ['git', 'fetch']
    if force:
        command.append('--force')
    command.extend(
        [x for x in _format_opts(opts) if x not in ('-f', '--force')]
    )
    if remote:
        if not isinstance(remote, six.string_types):
            remote = str(remote)
        command.append(remote)
    if refspecs is not None:
        if isinstance(refspecs, (list, tuple)):
            refspec_list = []
            for item in refspecs:
                if not isinstance(item, six.string_types):
                    refspec_list.append(str(item))
                else:
                    refspec_list.append(item)
        else:
            if not isinstance(refspecs, six.string_types):
                refspecs = str(refspecs)
            refspec_list = refspecs.split(',')
        command.extend(refspec_list)
    output = _git_run(command,
                      cwd=cwd,
                      runas=user,
                      identity=identity,
                      ignore_retcode=ignore_retcode,
                      redirect_stderr=True)['stdout']

    update_re = re.compile(
        r'[\s*]*(?:([0-9a-f]+)\.\.([0-9a-f]+)|'
        r'\[(?:new (tag|branch)|tag update)\])\s+(.+)->'
    )
    ret = {}
    for line in salt.utils.itertools.split(output, '\n'):
        match = update_re.match(line)
        if match:
            old_sha, new_sha, new_ref_type, ref_name = \
                match.groups()
            ref_name = ref_name.rstrip()
            if new_ref_type is not None:
                # ref is a new tag/branch
                ref_key = 'new tags' \
                    if new_ref_type == 'tag' \
                    else 'new branches'
                ret.setdefault(ref_key, []).append(ref_name)
            elif old_sha is not None:
                # ref is a branch update
                ret.setdefault('updated branches', {})[ref_name] = \
                    {'old': old_sha, 'new': new_sha}
            else:
                # ref is an updated tag
                ret.setdefault('updated tags', []).append(ref_name)
    return ret


def init(cwd,
         bare=False,
         template=None,
         separate_git_dir=None,
         shared=None,
         opts='',
         user=None,
         ignore_retcode=False):
    '''
    Interface to `git-init(1)`_

    cwd
        The path to the directory to be initialized

    bare : False
        If ``True``, init a bare repository

        .. versionadded:: 2015.8.0

    template
        Set this argument to specify an alternate `template directory`_

        .. versionadded:: 2015.8.0

    separate_git_dir
        Set this argument to specify an alternate ``$GIT_DIR``

        .. versionadded:: 2015.8.0

    shared
        Set sharing permissions on git repo. See `git-init(1)`_ for more
        details.

        .. versionadded:: 2015.8.0

    opts
        Any additional options to add to the command line, in a single string

        .. note::
            On the Salt CLI, if the opts are preceded with a dash, it is
            necessary to precede them with ``opts=`` (as in the CLI examples
            below) to avoid causing errors with Salt's own argument parsing.

    user
        User under which to run the git command. By default, the command is run
        by the user under which the minion is running.

    ignore_retcode : False
        If ``True``, do not log an error to the minion log if the git command
        returns a nonzero exit status.

        .. versionadded:: 2015.8.0

    .. _`git-init(1)`: http://git-scm.com/docs/git-init
    .. _`template directory`: http://git-scm.com/docs/git-init#_template_directory


    CLI Examples:

    .. code-block:: bash

        salt myminion git.init /path/to/repo
        # Init a bare repo (before 2015.8.0)
        salt myminion git.init /path/to/bare/repo.git opts='--bare'
        # Init a bare repo (2015.8.0 and later)
        salt myminion git.init /path/to/bare/repo.git bare=True
    '''
    cwd = _expand_path(cwd, user)
    command = ['git', 'init']
    if bare:
        command.append('--bare')
    if template is not None:
        if not isinstance(template, six.string_types):
            template = str(template)
        command.append('--template={0}'.format(template))
    if separate_git_dir is not None:
        if not isinstance(separate_git_dir, six.string_types):
            separate_git_dir = str(separate_git_dir)
        command.append('--separate-git-dir={0}'.format(separate_git_dir))
    if shared is not None:
        if isinstance(shared, six.integer_types) \
                and not isinstance(shared, bool):
            shared = '0' + str(shared)
        elif not isinstance(shared, six.string_types):
            # Using lower here because booleans would be capitalized when
            # converted to a string.
            shared = str(shared).lower()
        command.append('--shared={0}'.format(shared))
    command.extend(_format_opts(opts))
    command.append(cwd)
    return _git_run(command,
                    runas=user,
                    ignore_retcode=ignore_retcode)['stdout']


def is_worktree(cwd, user=None):
    '''
    .. versionadded:: 2015.8.0

    This function will attempt to determine if ``cwd`` is part of a
    worktree by checking its ``.git`` to see if it is a file containing a
    reference to another gitdir.

    cwd
        path to the worktree to be removed

    user
        User under which to run the git command. By default, the command is run
        by the user under which the minion is running.


    CLI Example:

    .. code-block:: bash

        salt myminion git.is_worktree /path/to/repo
    '''
    cwd = _expand_path(cwd, user)
    try:
        toplevel = _get_toplevel(cwd)
    except CommandExecutionError:
        return False
    gitdir = os.path.join(toplevel, '.git')
    try:
        with salt.utils.fopen(gitdir, 'r') as fp_:
            for line in fp_:
                try:
                    label, path = line.split(None, 1)
                except ValueError:
                    return False
                else:
                    # This file should only contain a single line. However, we
                    # loop here to handle the corner case where .git is a large
                    # binary file, so that we do not read the entire file into
                    # memory at once. We'll hit a return statement before this
                    # loop enters a second iteration.
                    if label == 'gitdir:' and os.path.isabs(path):
                        return True
                    else:
                        return False
    except IOError:
        return False
    return False


def list_branches(cwd, remote=False, user=None, ignore_retcode=False):
    '''
    .. versionadded:: 2015.8.0

    Return a list of branches

    cwd
        The path to the git checkout

    remote : False
        If ``True``, list remote branches. Otherwise, local branches will be
        listed.

        .. warning::

            This option will only return remote branches of which the local
            checkout is aware, use :py:func:`git.fetch
            <salt.modules.git.fetch>` to update remotes.

    user
        User under which to run the git command. By default, the command is run
        by the user under which the minion is running.

    ignore_retcode : False
        If ``True``, do not log an error to the minion log if the git command
        returns a nonzero exit status.

        .. versionadded:: 2015.8.0


    CLI Examples:

    .. code-block:: bash

        salt myminion git.list_branches /path/to/repo
        salt myminion git.list_branches /path/to/repo remote=True
    '''
    cwd = _expand_path(cwd, user)
    command = ['git', 'for-each-ref', '--format', '%(refname:short)',
               'refs/{0}/'.format('heads' if not remote else 'remotes')]
    return _git_run(command,
                    cwd=cwd,
                    runas=user,
                    ignore_retcode=ignore_retcode)['stdout'].splitlines()


def list_tags(cwd, user=None, ignore_retcode=False):
    '''
    .. versionadded:: 2015.8.0

    Return a list of tags

    cwd
        The path to the git checkout

    user
        User under which to run the git command. By default, the command is run
        by the user under which the minion is running.

    ignore_retcode : False
        If ``True``, do not log an error to the minion log if the git command
        returns a nonzero exit status.

        .. versionadded:: 2015.8.0


    CLI Examples:

    .. code-block:: bash

        salt myminion git.list_tags /path/to/repo
    '''
    cwd = _expand_path(cwd, user)
    command = ['git', 'for-each-ref', '--format', '%(refname:short)',
               'refs/tags/']
    return _git_run(command,
                    cwd=cwd,
                    runas=user,
                    ignore_retcode=ignore_retcode)['stdout'].splitlines()


def list_worktrees(cwd, stale=False, user=None, **kwargs):
    '''
    .. versionadded:: 2015.8.0

    Returns information on worktrees

    .. versionchanged:: 2015.8.4
        Version 2.7.0 added the ``list`` subcommand to `git-worktree(1)`_ which
        provides a lot of additional information. The return data has been
        changed to include this information, even for pre-2.7.0 versions of
        git. In addition, if a worktree has a detached head, then any tags
        which point to the worktree's HEAD will be included in the return data.

    .. note::
        By default, only worktrees for which the worktree directory is still
        present are returned, but this can be changed using the ``all`` and
        ``stale`` arguments (described below).

    cwd
        The path to the git checkout

    user
        User under which to run the git command. By default, the command is run
        by the user under which the minion is running.

    all : False
        If ``True``, then return all worktrees tracked under
        $GIT_DIR/worktrees, including ones for which the gitdir is no longer
        present.

    stale : False
        If ``True``, return *only* worktrees whose gitdir is no longer present.

    .. note::
        Only one of ``all`` and ``stale`` can be set to ``True``.

    .. _`git-worktree(1)`: http://git-scm.com/docs/git-worktree


    CLI Examples:

    .. code-block:: bash

        salt myminion git.list_worktrees /path/to/repo
        salt myminion git.list_worktrees /path/to/repo all=True
        salt myminion git.list_worktrees /path/to/repo stale=True
    '''
    if not _check_worktree_support(failhard=True):
        return {}
    cwd = _expand_path(cwd, user)
    kwargs = salt.utils.clean_kwargs(**kwargs)
    all_ = kwargs.pop('all', False)
    if kwargs:
        salt.utils.invalid_kwargs(kwargs)

    if all_ and stale:
        raise CommandExecutionError(
            '\'all\' and \'stale\' cannot both be set to True'
        )

    def _git_tag_points_at(cwd, rev, user=None):
        '''
        Get any tags that point at a
        '''
        return _git_run(['git', 'tag', '--points-at', rev],
                        cwd=cwd,
                        runas=user)['stdout'].splitlines()

    def _desired(is_stale, all_, stale):
        '''
        Common logic to determine whether or not to include the worktree info
        in the return data.
        '''
        if is_stale:
            if not all_ and not stale:
                # Stale worktrees are not desired, skip this one
                return False
        else:
            if stale:
                # Only stale worktrees are desired, skip this one
                return False
        return True

    def _duplicate_worktree_path(path):
        '''
        Log errors to the minion log notifying of duplicate worktree paths.
        These should not be there, but may show up due to a bug in git 2.7.0.
        '''
        log.error(
            'git.worktree: Duplicate worktree path {0}. This may be caused by '
            'a known issue in git 2.7.0 (see '
            'http://permalink.gmane.org/gmane.comp.version-control.git/283998)'
            .format(path)
        )

    tracked_data_points = ('worktree', 'HEAD', 'branch')
    ret = {}
    git_version = _LooseVersion(version(versioninfo=False))
    has_native_list_subcommand = git_version >= _LooseVersion('2.7.0')
    if has_native_list_subcommand:
        out = _git_run(['git', 'worktree', 'list', '--porcelain'],
                       cwd=cwd,
                       runas=user)
        if out['retcode'] != 0:
            msg = 'Failed to list worktrees'
            if out['stderr']:
                msg += ': {0}'.format(out['stderr'])
            raise CommandExecutionError(msg)

        def _untracked_item(line):
            '''
            Log a warning
            '''
            log.warning(
                'git.worktree: Untracked line item \'{0}\''.format(line)
            )

        for individual_worktree in \
                salt.utils.itertools.split(out['stdout'].strip(), '\n\n'):
            # Initialize the dict where we're storing the tracked data points
            worktree_data = dict([(x, '') for x in tracked_data_points])

            for line in salt.utils.itertools.split(individual_worktree, '\n'):
                try:
                    type_, value = line.strip().split(None, 1)
                except ValueError:
                    if line == 'detached':
                        type_ = 'branch'
                        value = 'detached'
                    else:
                        _untracked_item(line)
                        continue

                if type_ not in tracked_data_points:
                    _untracked_item(line)
                    continue

                if worktree_data[type_]:
                    log.error(
                        'git.worktree: Unexpected duplicate {0} entry '
                        '\'{1}\', skipping'.format(type_, line)
                    )
                    continue

                worktree_data[type_] = value

            # Check for missing data points
            missing = [x for x in tracked_data_points if not worktree_data[x]]
            if missing:
                log.error(
                    'git.worktree: Incomplete worktree data, missing the '
                    'following information: {0}. Full data below:\n{1}'
                    .format(', '.join(missing), individual_worktree)
                )
                continue

            worktree_is_stale = not os.path.isdir(worktree_data['worktree'])

            if not _desired(worktree_is_stale, all_, stale):
                continue

            if worktree_data['worktree'] in ret:
                _duplicate_worktree_path(worktree_data['worktree'])

            wt_ptr = ret.setdefault(worktree_data['worktree'], {})
            wt_ptr['stale'] = worktree_is_stale
            wt_ptr['HEAD'] = worktree_data['HEAD']
            wt_ptr['detached'] = worktree_data['branch'] == 'detached'
            if wt_ptr['detached']:
                wt_ptr['branch'] = None
                # Check to see if HEAD points at a tag
                tags_found = _git_tag_points_at(cwd, wt_ptr['HEAD'], user)
                if tags_found:
                    wt_ptr['tags'] = tags_found
            else:
                wt_ptr['branch'] = \
                    worktree_data['branch'].replace('refs/heads/', '', 1)

        return ret

    else:
        toplevel = _get_toplevel(cwd, user)
        try:
            worktree_root = rev_parse(cwd,
                                      opts=['--git-path', 'worktrees'],
                                      user=user)
        except CommandExecutionError as exc:
            msg = 'Failed to find worktree location for ' + cwd
            log.error(msg, exc_info_on_loglevel=logging.DEBUG)
            raise CommandExecutionError(msg)
        if worktree_root.startswith('.git'):
            worktree_root = os.path.join(cwd, worktree_root)
        if not os.path.isdir(worktree_root):
            raise CommandExecutionError(
                'Worktree admin directory {0} not present'
                .format(worktree_root)
            )

        def _read_file(path):
            '''
            Return contents of a single line file with EOF newline stripped
            '''
            try:
                with salt.utils.fopen(path, 'r') as fp_:
                    for line in fp_:
                        ret = line.strip()
                        # Ignore other lines, if they exist (which they
                        # shouldn't)
                        break
                    return ret
            except (IOError, OSError) as exc:
                # Raise a CommandExecutionError
                salt.utils.files.process_read_exception(exc, path)

        for worktree_name in os.listdir(worktree_root):
            admin_dir = os.path.join(worktree_root, worktree_name)
            gitdir_file = os.path.join(admin_dir, 'gitdir')
            head_file = os.path.join(admin_dir, 'HEAD')

            wt_loc = _read_file(gitdir_file)
            head_ref = _read_file(head_file)

            if not os.path.isabs(wt_loc):
                log.error(
                    'Non-absolute path found in {0}. If git 2.7.0 was '
                    'installed and then downgraded, this was likely caused '
                    'by a known issue in git 2.7.0. See '
                    'http://permalink.gmane.org/gmane.comp.version-control'
                    '.git/283998 for more information.'.format(gitdir_file)
                )
                # Emulate what 'git worktree list' does under-the-hood, and
                # that is using the toplevel directory. It will still give
                # inaccurate results, but will avoid a traceback.
                wt_loc = toplevel

            if wt_loc.endswith('/.git'):
                wt_loc = wt_loc[:-5]

            worktree_is_stale = not os.path.isdir(wt_loc)

            if not _desired(worktree_is_stale, all_, stale):
                continue

            if wt_loc in ret:
                _duplicate_worktree_path(wt_loc)

            if head_ref.startswith('ref: '):
                head_ref = head_ref.split(None, 1)[-1]
                wt_branch = head_ref.replace('refs/heads/', '', 1)
                wt_head = rev_parse(cwd, rev=head_ref, user=user)
                wt_detached = False
            else:
                wt_branch = None
                wt_head = head_ref
                wt_detached = True

            wt_ptr = ret.setdefault(wt_loc, {})
            wt_ptr['stale'] = worktree_is_stale
            wt_ptr['branch'] = wt_branch
            wt_ptr['HEAD'] = wt_head
            wt_ptr['detached'] = wt_detached

            # Check to see if HEAD points at a tag
            if wt_detached:
                tags_found = _git_tag_points_at(cwd, wt_head, user)
                if tags_found:
                    wt_ptr['tags'] = tags_found

    return ret


def ls_remote(cwd=None,
              remote='origin',
              ref=None,
              opts='',
              user=None,
              identity=None,
              https_user=None,
              https_pass=None,
              ignore_retcode=False):
    '''
    Interface to `git-ls-remote(1)`_. Returns the upstream hash for a remote
    reference.

    cwd
        The path to the git checkout. Optional (and ignored if present) when
        ``remote`` is set to a URL instead of a remote name.

    remote : origin
        The name of the remote to query. Can be the name of a git remote
        (which exists in the git checkout defined by the ``cwd`` parameter),
        or the URL of a remote repository.

        .. versionchanged:: 2015.8.0
            Argument renamed from ``repository`` to ``remote``

    ref
        The name of the ref to query. Optional, if not specified, all refs are
        returned. Can be a branch or tag name, or the full name of the
        reference (for example, to get the hash for a Github pull request number
        1234, ``ref`` can be set to ``refs/pull/1234/head``

        .. versionchanged:: 2015.8.0
            Argument renamed from ``branch`` to ``ref``

        .. versionchanged:: 2015.8.4
            Defaults to returning all refs instead of master.

    opts
        Any additional options to add to the command line, in a single string

        .. versionadded:: 2015.8.0

    user
        User under which to run the git command. By default, the command is run
        by the user under which the minion is running.

    identity
        Path to a private key to use for ssh URLs

        .. warning::

            Unless Salt is invoked from the minion using ``salt-call``, the
            key(s) must be passphraseless. For greater security with
            passphraseless private keys, see the `sshd(8)`_ manpage for
            information on securing the keypair from the remote side in the
            ``authorized_keys`` file.

            .. _`sshd(8)`: http://www.man7.org/linux/man-pages/man8/sshd.8.html#AUTHORIZED_KEYS_FILE%20FORMAT

        .. versionchanged:: 2015.8.7
            Salt will no longer attempt to use passphrase-protected keys unless
            invoked from the minion using ``salt-call``, to prevent blocking
            waiting for user input.

    https_user
        Set HTTP Basic Auth username. Only accepted for HTTPS URLs.

        .. versionadded:: 2015.5.0

    https_pass
        Set HTTP Basic Auth password. Only accepted for HTTPS URLs.

        .. versionadded:: 2015.5.0

    ignore_retcode : False
        If ``True``, do not log an error to the minion log if the git command
        returns a nonzero exit status.

        .. versionadded:: 2015.8.0

    .. _`git-ls-remote(1)`: http://git-scm.com/docs/git-ls-remote


    CLI Example:

    .. code-block:: bash

        salt myminion git.ls_remote /path/to/repo origin master
        salt myminion git.ls_remote remote=https://mydomain.tld/repo.git ref=mytag opts='--tags'
    '''
    if cwd is not None:
        cwd = _expand_path(cwd, user)
    try:
        remote = salt.utils.url.add_http_basic_auth(remote,
                                                    https_user,
                                                    https_pass,
                                                    https_only=True)
    except ValueError as exc:
        raise SaltInvocationError(exc.__str__())
    command = ['git', 'ls-remote']
    command.extend(_format_opts(opts))
    if not isinstance(remote, six.string_types):
        remote = str(remote)
    command.extend([remote])
    if ref:
        if not isinstance(ref, six.string_types):
            ref = str(ref)
        command.extend([ref])
    output = _git_run(command,
                      cwd=cwd,
                      runas=user,
                      identity=identity,
                      ignore_retcode=ignore_retcode)['stdout']
    ret = {}
    for line in output.splitlines():
        try:
            ref_sha1, ref_name = line.split(None, 1)
        except IndexError:
            continue
        ret[ref_name] = ref_sha1
    return ret


def merge(cwd,
          rev=None,
          opts='',
          user=None,
          ignore_retcode=False,
          **kwargs):
    '''
    Interface to `git-merge(1)`_

    cwd
        The path to the git checkout

    rev
        Revision to merge into the current branch. If not specified, the remote
        tracking branch will be merged.

        .. versionadded:: 2015.8.0

    branch
        The remote branch or revision to merge into the current branch
        Revision to merge into the current branch

        .. deprecated:: 2015.8.0
            Use ``rev`` instead.

    opts
        Any additional options to add to the command line, in a single string

        .. note::
            On the Salt CLI, if the opts are preceded with a dash, it is
            necessary to precede them with ``opts=`` (as in the CLI examples
            below) to avoid causing errors with Salt's own argument parsing.

    user
        User under which to run the git command. By default, the command is run
        by the user under which the minion is running.

    ignore_retcode : False
        If ``True``, do not log an error to the minion log if the git command
        returns a nonzero exit status.

        .. versionadded:: 2015.8.0

    .. _`git-merge(1)`: http://git-scm.com/docs/git-merge


    CLI Example:

    .. code-block:: bash

        # Fetch first...
        salt myminion git.fetch /path/to/repo
        # ... then merge the remote tracking branch
        salt myminion git.merge /path/to/repo
        # .. or merge another rev
        salt myminion git.merge /path/to/repo rev=upstream/foo
    '''
    kwargs = salt.utils.clean_kwargs(**kwargs)
    branch_ = kwargs.pop('branch', None)
    if kwargs:
        salt.utils.invalid_kwargs(kwargs)

    cwd = _expand_path(cwd, user)
    if branch_:
        salt.utils.warn_until(
            'Nitrogen',
            'The \'branch\' argument to git.merge has been deprecated, please '
            'use \'rev\' instead.'
        )
        rev = branch_
    command = ['git', 'merge']
    command.extend(_format_opts(opts))
    if rev:
        if not isinstance(rev, six.string_types):
            rev = str(rev)
        command.append(rev)
    return _git_run(command,
                    cwd=cwd,
                    runas=user,
                    ignore_retcode=ignore_retcode)['stdout']


def merge_base(cwd,
               refs=None,
               octopus=False,
               is_ancestor=False,
               independent=False,
               fork_point=None,
               opts='',
               user=None,
               ignore_retcode=False,
               **kwargs):
    '''
    .. versionadded:: 2015.8.0

    Interface to `git-merge-base(1)`_.

    cwd
        The path to the git checkout

    refs
        Any refs/commits to check for a merge base. Can be passed as a
        comma-separated list or a Python list.

    all : False
        Return a list of all matching merge bases. Not compatible with any of
        the below options except for ``octopus``.

    octopus : False
        If ``True``, then this function will determine the best common
        ancestors of all specified commits, in preparation for an n-way merge.
        See here_ for a description of how these bases are determined.

        Set ``all`` to ``True`` with this option to return all computed merge
        bases, otherwise only the "best" will be returned.

    is_ancestor : False
        If ``True``, then instead of returning the merge base, return a
        boolean telling whether or not the first commit is an ancestor of the
        second commit.

        .. note::
            This option requires two commits to be passed.

        .. versionchanged:: 2015.8.2
            Works properly in git versions older than 1.8.0, where the
            ``--is-ancestor`` CLI option is not present.

    independent : False
        If ``True``, this function will return the IDs of the refs/commits
        passed which cannot be reached by another commit.

    fork_point
        If passed, then this function will return the commit where the
        commit diverged from the ref specified by ``fork_point``. If no fork
        point is found, ``None`` is returned.

        .. note::
            At most one commit is permitted to be passed if a ``fork_point`` is
            specified. If no commits are passed, then ``HEAD`` is assumed.

    opts
        Any additional options to add to the command line, in a single string

        .. note::
            On the Salt CLI, if the opts are preceded with a dash, it is
            necessary to precede them with ``opts=`` (as in the CLI examples
            below) to avoid causing errors with Salt's own argument parsing.

            This option should not be necessary unless new CLI arguments are
            added to `git-merge-base(1)`_ and are not yet supported in Salt.

    user
        User under which to run the git command. By default, the command is run
        by the user under which the minion is running.

    ignore_retcode : False
        if ``True``, do not log an error to the minion log if the git command
        returns a nonzero exit status.

    .. _`git-merge-base(1)`: http://git-scm.com/docs/git-merge-base
    .. _here: http://git-scm.com/docs/git-merge-base#_discussion


    CLI Examples:

    .. code-block:: bash

        salt myminion git.merge_base /path/to/repo HEAD upstream/mybranch
        salt myminion git.merge_base /path/to/repo 8f2e542,4ad8cab,cdc9886 octopus=True
        salt myminion git.merge_base /path/to/repo refs=8f2e542,4ad8cab,cdc9886 independent=True
        salt myminion git.merge_base /path/to/repo refs=8f2e542,4ad8cab is_ancestor=True
        salt myminion git.merge_base /path/to/repo fork_point=upstream/master
        salt myminion git.merge_base /path/to/repo refs=mybranch fork_point=upstream/master
    '''
    cwd = _expand_path(cwd, user)
    kwargs = salt.utils.clean_kwargs(**kwargs)
    all_ = kwargs.pop('all', False)
    if kwargs:
        salt.utils.invalid_kwargs(kwargs)

    if all_ and (independent or is_ancestor or fork_point):
        raise SaltInvocationError(
            'The \'all\' argument is not compatible with \'independent\', '
            '\'is_ancestor\', or \'fork_point\''
        )

    if refs is None:
        refs = []
    elif not isinstance(refs, (list, tuple)):
        refs = [x.strip() for x in str(refs).split(',')]
    mutually_exclusive_count = len(
        [x for x in (octopus, independent, is_ancestor, fork_point) if x]
    )
    if mutually_exclusive_count > 1:
        raise SaltInvocationError(
            'Only one of \'octopus\', \'independent\', \'is_ancestor\', and '
            '\'fork_point\' is permitted'
        )
    elif is_ancestor:
        if len(refs) != 2:
            raise SaltInvocationError(
                'Two refs/commits are required if \'is_ancestor\' is True'
            )
    elif fork_point:
        if len(refs) > 1:
            raise SaltInvocationError(
                'At most one ref/commit can be passed if \'fork_point\' is '
                'specified'
            )
        elif not refs:
            refs = ['HEAD']
        if not isinstance(fork_point, six.string_types):
            fork_point = str(fork_point)

    if is_ancestor:
        if _LooseVersion(version(versioninfo=False)) < _LooseVersion('1.8.0'):
            # Pre 1.8.0 git doesn't have --is-ancestor, so the logic here is a
            # little different. First we need to resolve the first ref to a
            # full SHA1, and then if running git merge-base on both commits
            # returns an identical commit to the resolved first ref, we know
            # that the first ref is an ancestor of the second ref.
            first_commit = rev_parse(cwd,
                                     rev=refs[0],
                                     opts=['--verify'],
                                     user=user,
                                     ignore_retcode=ignore_retcode)
            return merge_base(cwd,
                              refs=refs,
                              is_ancestor=False,
                              user=user,
                              ignore_retcode=ignore_retcode) == first_commit

    command = ['git', 'merge-base']
    command.extend(_format_opts(opts))
    if all_:
        command.append('--all')
    if octopus:
        command.append('--octopus')
    elif is_ancestor:
        command.append('--is-ancestor')
    elif independent:
        command.append('--independent')
    elif fork_point:
        command.extend(['--fork-point', fork_point])
    for ref in refs:
        if isinstance(ref, six.string_types):
            command.append(ref)
        else:
            command.append(str(ref))
    result = _git_run(command,
                      cwd=cwd,
                      runas=user,
                      ignore_retcode=ignore_retcode,
                      failhard=False if is_ancestor else True)
    if is_ancestor:
        return result['retcode'] == 0
    all_bases = result['stdout'].splitlines()
    if all_:
        return all_bases
    return all_bases[0]


def merge_tree(cwd,
               ref1,
               ref2,
               base=None,
               user=None,
               ignore_retcode=False):
    '''
    .. versionadded:: 2015.8.0

    Interface to `git-merge-tree(1)`_, shows the merge results and conflicts
    from a 3-way merge without touching the index.

    cwd
        The path to the git checkout

    ref1
        First ref/commit to compare

    ref2
        Second ref/commit to compare

    base
        The base tree to use for the 3-way-merge. If not provided, then
        :py:func:`git.merge_base <salt.modules.git.merge_base>` will be invoked
        on ``ref1`` and ``ref2`` to determine the merge base to use.

    user
        User under which to run the git command. By default, the command is run
        by the user under which the minion is running.

    ignore_retcode : False
        if ``True``, do not log an error to the minion log if the git command
        returns a nonzero exit status.

    .. _`git-merge-tree(1)`: http://git-scm.com/docs/git-merge-tree


    CLI Examples:

    .. code-block:: bash

        salt myminion git.merge_tree /path/to/repo HEAD upstream/dev
        salt myminion git.merge_tree /path/to/repo HEAD upstream/dev base=aaf3c3d
    '''
    cwd = _expand_path(cwd, user)
    command = ['git', 'merge-tree']
    if not isinstance(ref1, six.string_types):
        ref1 = str(ref1)
    if not isinstance(ref2, six.string_types):
        ref2 = str(ref2)
    if base is None:
        try:
            base = merge_base(cwd, refs=[ref1, ref2])
        except (SaltInvocationError, CommandExecutionError):
            raise CommandExecutionError(
                'Unable to determine merge base for {0} and {1}'
                .format(ref1, ref2)
            )
    command.extend([base, ref1, ref2])
    return _git_run(command,
                    cwd=cwd,
                    runas=user,
                    ignore_retcode=ignore_retcode)['stdout']


def pull(cwd, opts='', user=None, identity=None, ignore_retcode=False):
    '''
    Interface to `git-pull(1)`_

    cwd
        The path to the git checkout

    opts
        Any additional options to add to the command line, in a single string

        .. note::
            On the Salt CLI, if the opts are preceded with a dash, it is
            necessary to precede them with ``opts=`` (as in the CLI examples
            below) to avoid causing errors with Salt's own argument parsing.

    user
        User under which to run the git command. By default, the command is run
        by the user under which the minion is running.

    identity
        Path to a private key to use for ssh URLs

        .. warning::

            Unless Salt is invoked from the minion using ``salt-call``, the
            key(s) must be passphraseless. For greater security with
            passphraseless private keys, see the `sshd(8)`_ manpage for
            information on securing the keypair from the remote side in the
            ``authorized_keys`` file.

            .. _`sshd(8)`: http://www.man7.org/linux/man-pages/man8/sshd.8.html#AUTHORIZED_KEYS_FILE%20FORMAT

        .. versionchanged:: 2015.8.7
            Salt will no longer attempt to use passphrase-protected keys unless
            invoked from the minion using ``salt-call``, to prevent blocking
            waiting for user input.

    ignore_retcode : False
        If ``True``, do not log an error to the minion log if the git command
        returns a nonzero exit status.

        .. versionadded:: 2015.8.0

    .. _`git-pull(1)`: http://git-scm.com/docs/git-pull

    CLI Example:

    .. code-block:: bash

        salt myminion git.pull /path/to/repo opts='--rebase origin master'
    '''
    cwd = _expand_path(cwd, user)
    command = ['git', 'pull']
    command.extend(_format_opts(opts))
    return _git_run(command,
                    cwd=cwd,
                    runas=user,
                    identity=identity,
                    ignore_retcode=ignore_retcode)['stdout']


def push(cwd,
         remote=None,
         ref=None,
         opts='',
         user=None,
         identity=None,
         ignore_retcode=False,
         **kwargs):
    '''
    Interface to `git-push(1)`_

    cwd
        The path to the git checkout

    remote
        Name of the remote to which the ref should being pushed

        .. versionadded:: 2015.8.0

    ref : master
        Name of the ref to push

        .. note::
            Being a refspec_, this argument can include a colon to define local
            and remote ref names.

    branch
        Name of the ref to push

        .. deprecated:: 2015.8.0
            Use ``ref`` instead

    opts
        Any additional options to add to the command line, in a single string

        .. note::
            On the Salt CLI, if the opts are preceded with a dash, it is
            necessary to precede them with ``opts=`` (as in the CLI examples
            below) to avoid causing errors with Salt's own argument parsing.

    user
        User under which to run the git command. By default, the command is run
        by the user under which the minion is running.

    identity
        Path to a private key to use for ssh URLs

        .. warning::

            Unless Salt is invoked from the minion using ``salt-call``, the
            key(s) must be passphraseless. For greater security with
            passphraseless private keys, see the `sshd(8)`_ manpage for
            information on securing the keypair from the remote side in the
            ``authorized_keys`` file.

            .. _`sshd(8)`: http://www.man7.org/linux/man-pages/man8/sshd.8.html#AUTHORIZED_KEYS_FILE%20FORMAT

        .. versionchanged:: 2015.8.7
            Salt will no longer attempt to use passphrase-protected keys unless
            invoked from the minion using ``salt-call``, to prevent blocking
            waiting for user input.

    ignore_retcode : False
        If ``True``, do not log an error to the minion log if the git command
        returns a nonzero exit status.

        .. versionadded:: 2015.8.0

    .. _`git-push(1)`: http://git-scm.com/docs/git-push
    .. _refspec: http://git-scm.com/book/en/v2/Git-Internals-The-Refspec

    CLI Example:

    .. code-block:: bash

        # Push master as origin/master
        salt myminion git.push /path/to/repo origin master
        # Push issue21 as upstream/develop
        salt myminion git.push /path/to/repo upstream issue21:develop
        # Delete remote branch 'upstream/temp'
        salt myminion git.push /path/to/repo upstream :temp
    '''
    kwargs = salt.utils.clean_kwargs(**kwargs)
    branch_ = kwargs.pop('branch', None)
    if kwargs:
        salt.utils.invalid_kwargs(kwargs)

    cwd = _expand_path(cwd, user)
    if branch_:
        salt.utils.warn_until(
            'Nitrogen',
            'The \'branch\' argument to git.push has been deprecated, please '
            'use \'ref\' instead.'
        )
        ref = branch_
    command = ['git', 'push']
    command.extend(_format_opts(opts))
    if not isinstance(remote, six.string_types):
        remote = str(remote)
    if not isinstance(ref, six.string_types):
        ref = str(ref)
    command.extend([remote, ref])
    return _git_run(command,
                    cwd=cwd,
                    runas=user,
                    identity=identity,
                    ignore_retcode=ignore_retcode)['stdout']


def rebase(cwd, rev='master', opts='', user=None, ignore_retcode=False):
    '''
    Interface to `git-rebase(1)`_

    cwd
        The path to the git checkout

    rev : master
        The revision to rebase onto the current branch

    opts
        Any additional options to add to the command line, in a single string

        .. note::
            On the Salt CLI, if the opts are preceded with a dash, it is
            necessary to precede them with ``opts=`` (as in the CLI examples
            below) to avoid causing errors with Salt's own argument parsing.

    user
        User under which to run the git command. By default, the command is run
        by the user under which the minion is running.

    ignore_retcode : False
        If ``True``, do not log an error to the minion log if the git command
        returns a nonzero exit status.

        .. versionadded:: 2015.8.0

    .. _`git-rebase(1)`: http://git-scm.com/docs/git-rebase


    CLI Example:

    .. code-block:: bash

        salt myminion git.rebase /path/to/repo master
        salt myminion git.rebase /path/to/repo 'origin master'
        salt myminion git.rebase /path/to/repo origin/master opts='--onto newbranch'
    '''
    cwd = _expand_path(cwd, user)
    opts = _format_opts(opts)
    if any(x for x in opts if x in ('-i', '--interactive')):
        raise SaltInvocationError('Interactive rebases are not supported')
    command = ['git', 'rebase']
    command.extend(opts)
    if not isinstance(rev, six.string_types):
        rev = str(rev)
    command.extend(shlex.split(rev))
    return _git_run(command,
                    cwd=cwd,
                    runas=user,
                    ignore_retcode=ignore_retcode)['stdout']


def remote_get(cwd,
               remote='origin',
               user=None,
               redact_auth=True,
               ignore_retcode=False):
    '''
    Get the fetch and push URL for a specific remote

    cwd
        The path to the git checkout

    remote : origin
        Name of the remote to query

    user
        User under which to run the git command. By default, the command is run
        by the user under which the minion is running.

    redact_auth : True
        Set to ``False`` to include the username/password if the remote uses
        HTTPS Basic Auth. Otherwise, this information will be redacted.

        .. warning::
            Setting this to ``False`` will not only reveal any HTTPS Basic Auth
            that is configured, but the return data will also be written to the
            job cache. When possible, it is recommended to use SSH for
            authentication.

        .. versionadded:: 2015.5.6

    ignore_retcode : False
        If ``True``, do not log an error to the minion log if the git command
        returns a nonzero exit status.

        .. versionadded:: 2015.8.0


    CLI Examples:

    .. code-block:: bash

        salt myminion git.remote_get /path/to/repo
        salt myminion git.remote_get /path/to/repo upstream
    '''
    cwd = _expand_path(cwd, user)
    all_remotes = remotes(cwd,
                          user=user,
                          redact_auth=redact_auth,
                          ignore_retcode=ignore_retcode)
    if remote not in all_remotes:
        raise CommandExecutionError(
            'Remote \'{0}\' not present in git checkout located at {1}'
            .format(remote, cwd)
        )
    return all_remotes[remote]


def remote_refs(url,
                heads=False,
                tags=False,
                user=None,
                identity=None,
                https_user=None,
                https_pass=None,
                ignore_retcode=False):
    '''
    .. versionadded:: 2015.8.0

    Return the remote refs for the specified URL

    url
        URL of the remote repository

    heads : False
        Restrict output to heads. Can be combined with ``tags``.

    tags : False
        Restrict output to tags. Can be combined with ``heads``.

    user
        User under which to run the git command. By default, the command is run
        by the user under which the minion is running.

    identity
        Path to a private key to use for ssh URLs

        .. warning::

            Unless Salt is invoked from the minion using ``salt-call``, the
            key(s) must be passphraseless. For greater security with
            passphraseless private keys, see the `sshd(8)`_ manpage for
            information on securing the keypair from the remote side in the
            ``authorized_keys`` file.

            .. _`sshd(8)`: http://www.man7.org/linux/man-pages/man8/sshd.8.html#AUTHORIZED_KEYS_FILE%20FORMAT

        .. versionchanged:: 2015.8.7
            Salt will no longer attempt to use passphrase-protected keys unless
            invoked from the minion using ``salt-call``, to prevent blocking
            waiting for user input.

    https_user
        Set HTTP Basic Auth username. Only accepted for HTTPS URLs.

    https_pass
        Set HTTP Basic Auth password. Only accepted for HTTPS URLs.

    ignore_retcode : False
        If ``True``, do not log an error to the minion log if the git command
        returns a nonzero exit status.

    CLI Example:

    .. code-block:: bash

        salt myminion git.remote_refs https://github.com/saltstack/salt.git
    '''
    command = ['git', 'ls-remote']
    if heads:
        command.append('--heads')
    if tags:
        command.append('--tags')
    try:
        command.append(salt.utils.url.add_http_basic_auth(url,
                                                          https_user,
                                                          https_pass,
                                                          https_only=True))
    except ValueError as exc:
        raise SaltInvocationError(exc.__str__())
    output = _git_run(command,
                      runas=user,
                      identity=identity,
                      ignore_retcode=ignore_retcode)['stdout']
    ret = {}
    for line in salt.utils.itertools.split(output, '\n'):
        try:
            sha1_hash, ref_name = line.split(None, 1)
        except ValueError:
            continue
        ret[ref_name] = sha1_hash
    return ret


def remote_set(cwd,
               url,
               remote='origin',
               user=None,
               https_user=None,
               https_pass=None,
               push_url=None,
               push_https_user=None,
               push_https_pass=None,
               ignore_retcode=False):
    '''
    cwd
        The path to the git checkout

    url
        Remote URL to set

    remote : origin
        Name of the remote to set

    push_url
        If unset, the push URL will be identical to the fetch URL.

        .. versionadded:: 2015.8.0

    user
        User under which to run the git command. By default, the command is run
        by the user under which the minion is running.

    https_user
        Set HTTP Basic Auth username. Only accepted for HTTPS URLs.

        .. versionadded:: 2015.5.0

    https_pass
        Set HTTP Basic Auth password. Only accepted for HTTPS URLs.

        .. versionadded:: 2015.5.0

    push_https_user
        Set HTTP Basic Auth user for ``push_url``. Ignored if ``push_url`` is
        unset. Only accepted for HTTPS URLs.

        .. versionadded:: 2015.8.0

    push_https_pass
        Set HTTP Basic Auth password for ``push_url``. Ignored if ``push_url``
        is unset. Only accepted for HTTPS URLs.

        .. versionadded:: 2015.8.0

    ignore_retcode : False
        If ``True``, do not log an error to the minion log if the git command
        returns a nonzero exit status.

        .. versionadded:: 2015.8.0


    CLI Examples:

    .. code-block:: bash

        salt myminion git.remote_set /path/to/repo git@github.com:user/repo.git
        salt myminion git.remote_set /path/to/repo git@github.com:user/repo.git remote=upstream
        salt myminion git.remote_set /path/to/repo https://github.com/user/repo.git remote=upstream push_url=git@github.com:user/repo.git
    '''
    # Check if remote exists
    if remote in remotes(cwd, user=user):
        log.debug(
            'Remote \'{0}\' already exists in git checkout located at {1}, '
            'removing so it can be re-added'.format(remote, cwd)
        )
        command = ['git', 'remote', 'rm', remote]
        _git_run(command, cwd=cwd, runas=user, ignore_retcode=ignore_retcode)
    # Add remote
    try:
        url = salt.utils.url.add_http_basic_auth(url,
                                                 https_user,
                                                 https_pass,
                                                 https_only=True)
    except ValueError as exc:
        raise SaltInvocationError(exc.__str__())
    if not isinstance(remote, six.string_types):
        remote = str(remote)
    if not isinstance(url, six.string_types):
        url = str(url)
    command = ['git', 'remote', 'add', remote, url]
    _git_run(command, cwd=cwd, runas=user, ignore_retcode=ignore_retcode)
    if push_url:
        if not isinstance(push_url, six.string_types):
            push_url = str(push_url)
        try:
            push_url = salt.utils.url.add_http_basic_auth(push_url,
                                                          push_https_user,
                                                          push_https_pass,
                                                          https_only=True)
        except ValueError as exc:
            raise SaltInvocationError(exc.__str__())
        command = ['git', 'remote', 'set-url', '--push', remote, push_url]
        _git_run(command, cwd=cwd, runas=user, ignore_retcode=ignore_retcode)
    return remote_get(cwd=cwd,
                      remote=remote,
                      user=user,
                      ignore_retcode=ignore_retcode)


def remotes(cwd, user=None, redact_auth=True, ignore_retcode=False):
    '''
    Get fetch and push URLs for each remote in a git checkout

    cwd
        The path to the git checkout

    user
        User under which to run the git command. By default, the command is run
        by the user under which the minion is running.

    redact_auth : True
        Set to ``False`` to include the username/password for authenticated
        remotes in the return data. Otherwise, this information will be
        redacted.

        .. warning::
            Setting this to ``False`` will not only reveal any HTTPS Basic Auth
            that is configured, but the return data will also be written to the
            job cache. When possible, it is recommended to use SSH for
            authentication.

        .. versionadded:: 2015.5.6

    ignore_retcode : False
        If ``True``, do not log an error to the minion log if the git command
        returns a nonzero exit status.

        .. versionadded:: 2015.8.0


    CLI Example:

    .. code-block:: bash

        salt myminion git.remotes /path/to/repo
    '''
    cwd = _expand_path(cwd, user)
    command = ['git', 'remote', '--verbose']
    ret = {}
    output = _git_run(command,
                      cwd=cwd,
                      runas=user,
                      ignore_retcode=ignore_retcode)['stdout']
    for remote_line in salt.utils.itertools.split(output, '\n'):
        try:
            remote, remote_info = remote_line.split(None, 1)
        except ValueError:
            continue
        try:
            remote_url, action = remote_info.rsplit(None, 1)
        except ValueError:
            continue
        # Remove parenthesis
        action = action.lstrip('(').rstrip(')').lower()
        if action not in ('fetch', 'push'):
            log.warning(
                'Unknown action \'{0}\' for remote \'{1}\' in git checkout '
                'located in {2}'.format(action, remote, cwd)
            )
            continue
        if redact_auth:
            remote_url = salt.utils.url.redact_http_basic_auth(remote_url)
        ret.setdefault(remote, {})[action] = remote_url
    return ret


def reset(cwd, opts='', user=None, ignore_retcode=False):
    '''
    Interface to `git-reset(1)`_, returns the stdout from the git command

    cwd
        The path to the git checkout

    opts
        Any additional options to add to the command line, in a single string

        .. note::
            On the Salt CLI, if the opts are preceded with a dash, it is
            necessary to precede them with ``opts=`` (as in the CLI examples
            below) to avoid causing errors with Salt's own argument parsing.

    user
        User under which to run the git command. By default, the command is run
        by the user under which the minion is running.

    ignore_retcode : False
        If ``True``, do not log an error to the minion log if the git command
        returns a nonzero exit status.

        .. versionadded:: 2015.8.0

    .. _`git-reset(1)`: http://git-scm.com/docs/git-reset


    CLI Examples:

    .. code-block:: bash

        # Soft reset to a specific commit ID
        salt myminion git.reset /path/to/repo ac3ee5c
        # Hard reset
        salt myminion git.reset /path/to/repo opts='--hard origin/master'
    '''
    cwd = _expand_path(cwd, user)
    command = ['git', 'reset']
    command.extend(_format_opts(opts))
    return _git_run(command,
                    cwd=cwd,
                    runas=user,
                    ignore_retcode=ignore_retcode)['stdout']


def rev_parse(cwd, rev=None, opts='', user=None, ignore_retcode=False):
    '''
    .. versionadded:: 2015.8.0

    Interface to `git-rev-parse(1)`_

    cwd
        The path to the git checkout

    rev
        Revision to parse. See the `SPECIFYING REVISIONS`_ section of the
        `git-rev-parse(1)`_ manpage for details on how to format this argument.

        This argument is optional when using the options in the `Options for
        Files` section of the `git-rev-parse(1)`_ manpage.

    opts
        Any additional options to add to the command line, in a single string

    user
        User under which to run the git command. By default, the command is run
        by the user under which the minion is running.

    ignore_retcode : False
        If ``True``, do not log an error to the minion log if the git command
        returns a nonzero exit status.

    .. _`git-rev-parse(1)`: http://git-scm.com/docs/git-rev-parse
    .. _`SPECIFYING REVISIONS`: http://git-scm.com/docs/git-rev-parse#_specifying_revisions
    .. _`Options for Files`: http://git-scm.com/docs/git-rev-parse#_options_for_files


    CLI Examples:

    .. code-block:: bash

        # Get the full SHA1 for HEAD
        salt myminion git.rev_parse /path/to/repo HEAD
        # Get the short SHA1 for HEAD
        salt myminion git.rev_parse /path/to/repo HEAD opts='--short'
        # Get the develop branch's upstream tracking branch
        salt myminion git.rev_parse /path/to/repo 'develop@{upstream}' opts='--abbrev-ref'
        # Get the SHA1 for the commit corresponding to tag v1.2.3
        salt myminion git.rev_parse /path/to/repo 'v1.2.3^{commit}'
        # Find out whether or not the repo at /path/to/repo is a bare repository
        salt myminion git.rev_parse /path/to/repo opts='--is-bare-repository'
    '''
    cwd = _expand_path(cwd, user)
    command = ['git', 'rev-parse']
    command.extend(_format_opts(opts))
    if rev is not None:
        if not isinstance(rev, six.string_types):
            rev = str(rev)
        command.append(rev)
    return _git_run(command,
                    cwd=cwd,
                    runas=user,
                    ignore_retcode=ignore_retcode)['stdout']


def revision(cwd, rev='HEAD', short=False, user=None, ignore_retcode=False):
    '''
    Returns the SHA1 hash of a given identifier (hash, branch, tag, HEAD, etc.)

    cwd
        The path to the git checkout

    rev : HEAD
        The revision

    short : False
        If ``True``, return an abbreviated SHA1 git hash

    user
        User under which to run the git command. By default, the command is run
        by the user under which the minion is running.

    ignore_retcode : False
        If ``True``, do not log an error to the minion log if the git command
        returns a nonzero exit status.

        .. versionadded:: 2015.8.0

    CLI Example:

    .. code-block:: bash

        salt myminion git.revision /path/to/repo mybranch
    '''
    cwd = _expand_path(cwd, user)
    if not isinstance(rev, six.string_types):
        rev = str(rev)
    command = ['git', 'rev-parse']
    if short:
        command.append('--short')
    command.append(rev)
    return _git_run(command,
                    cwd=cwd,
                    runas=user,
                    ignore_retcode=ignore_retcode)['stdout']


def rm_(cwd, filename, opts='', user=None, ignore_retcode=False):
    '''
    Interface to `git-rm(1)`_

    cwd
        The path to the git checkout

    filename
        The location of the file/directory to remove, relative to ``cwd``

        .. note::
            To remove a directory, ``-r`` must be part of the ``opts``
            parameter.

    opts
        Any additional options to add to the command line, in a single string

        .. note::
            On the Salt CLI, if the opts are preceded with a dash, it is
            necessary to precede them with ``opts=`` (as in the CLI examples
            below) to avoid causing errors with Salt's own argument parsing.

    user
        User under which to run the git command. By default, the command is run
        by the user under which the minion is running.

    ignore_retcode : False
        If ``True``, do not log an error to the minion log if the git command
        returns a nonzero exit status.

        .. versionadded:: 2015.8.0

    .. _`git-rm(1)`: http://git-scm.com/docs/git-rm


    CLI Examples:

    .. code-block:: bash

        salt myminion git.rm /path/to/repo foo/bar.py
        salt myminion git.rm /path/to/repo foo/bar.py opts='--dry-run'
        salt myminion git.rm /path/to/repo foo/baz opts='-r'
    '''
    cwd = _expand_path(cwd, user)
    command = ['git', 'rm']
    command.extend(_format_opts(opts))
    command.extend(['--', filename])
    return _git_run(command,
                    cwd=cwd,
                    runas=user,
                    ignore_retcode=ignore_retcode)['stdout']


def stash(cwd, action='save', opts='', user=None, ignore_retcode=False):
    '''
    Interface to `git-stash(1)`_, returns the stdout from the git command

    cwd
        The path to the git checkout

    opts
        Any additional options to add to the command line, in a single string.
        Use this to complete the ``git stash`` command by adding the remaining
        arguments (i.e.  ``'save <stash comment>'``, ``'apply stash@{2}'``,
        ``'show'``, etc.).  Omitting this argument will simply run ``git
        stash``.

    user
        User under which to run the git command. By default, the command is run
        by the user under which the minion is running.

    ignore_retcode : False
        If ``True``, do not log an error to the minion log if the git command
        returns a nonzero exit status.

        .. versionadded:: 2015.8.0

    .. _`git-stash(1)`: http://git-scm.com/docs/git-stash


    CLI Examples:

    .. code-block:: bash

        salt myminion git.stash /path/to/repo save opts='work in progress'
        salt myminion git.stash /path/to/repo apply opts='stash@{1}'
        salt myminion git.stash /path/to/repo drop opts='stash@{1}'
        salt myminion git.stash /path/to/repo list
    '''
    cwd = _expand_path(cwd, user)
    if not isinstance(action, six.string_types):
        # No numeric actions but this will prevent a traceback when the git
        # command is run.
        action = str(action)
    command = ['git', 'stash', action]
    command.extend(_format_opts(opts))
    return _git_run(command,
                    cwd=cwd,
                    runas=user,
                    ignore_retcode=ignore_retcode)['stdout']


def status(cwd, user=None, ignore_retcode=False):
    '''
    .. versionchanged:: 2015.8.0
        Return data has changed from a list of lists to a dictionary

    Returns the changes to the repository

    cwd
        The path to the git checkout

    user
        User under which to run the git command. By default, the command is run
        by the user under which the minion is running.

    ignore_retcode : False
        If ``True``, do not log an error to the minion log if the git command
        returns a nonzero exit status.

        .. versionadded:: 2015.8.0


    CLI Example:

    .. code-block:: bash

        salt myminion git.status /path/to/repo
    '''
    cwd = _expand_path(cwd, user)
    state_map = {
        'M': 'modified',
        'A': 'new',
        'D': 'deleted',
        '??': 'untracked'
    }
    ret = {}
    command = ['git', 'status', '-z', '--porcelain']
    output = _git_run(command,
                      cwd=cwd,
                      runas=user,
                      ignore_retcode=ignore_retcode)['stdout']
    for line in output.split('\0'):
        try:
            state, filename = line.split(None, 1)
        except ValueError:
            continue
        ret.setdefault(state_map.get(state, state), []).append(filename)
    return ret


def submodule(cwd,
              command,
              opts='',
              user=None,
              identity=None,
              ignore_retcode=False,
              **kwargs):
    '''
    .. versionchanged:: 2015.8.0
        Added the ``command`` argument to allow for operations other than
        ``update`` to be run on submodules, and deprecated the ``init``
        argument. To do a submodule update with ``init=True`` moving forward,
        use ``command=update opts='--init'``

    Interface to `git-submodule(1)`_

    cwd
        The path to the submodule

    command
        Submodule command to run, see `git-submodule(1) <git submodule>` for
        more information. Any additional arguments after the command (such as
        the URL when adding a submodule) must be passed in the ``opts``
        parameter.

        .. versionadded:: 2015.8.0

    opts
        Any additional options to add to the command line, in a single string

        .. note::
            On the Salt CLI, if the opts are preceded with a dash, it is
            necessary to precede them with ``opts=`` (as in the CLI examples
            below) to avoid causing errors with Salt's own argument parsing.

    init : False
        If ``True``, ensures that new submodules are initialized

        .. deprecated:: 2015.8.0
            Pass ``init`` as the ``command`` parameter, or include ``--init``
            in the ``opts`` param with ``command`` set to update.

    user
        User under which to run the git command. By default, the command is run
        by the user under which the minion is running.

    identity
        Path to a private key to use for ssh URLs

        .. warning::

            Unless Salt is invoked from the minion using ``salt-call``, the
            key(s) must be passphraseless. For greater security with
            passphraseless private keys, see the `sshd(8)`_ manpage for
            information on securing the keypair from the remote side in the
            ``authorized_keys`` file.

            .. _`sshd(8)`: http://www.man7.org/linux/man-pages/man8/sshd.8.html#AUTHORIZED_KEYS_FILE%20FORMAT

        .. versionchanged:: 2015.8.7
            Salt will no longer attempt to use passphrase-protected keys unless
            invoked from the minion using ``salt-call``, to prevent blocking
            waiting for user input.

    ignore_retcode : False
        If ``True``, do not log an error to the minion log if the git command
        returns a nonzero exit status.

        .. versionadded:: 2015.8.0

    .. _`git-submodule(1)`: http://git-scm.com/docs/git-submodule

    CLI Example:

    .. code-block:: bash

        # Update submodule and ensure it is initialized (before 2015.8.0)
        salt myminion git.submodule /path/to/repo/sub/repo init=True
        # Update submodule and ensure it is initialized (2015.8.0 and later)
        salt myminion git.submodule /path/to/repo/sub/repo update opts='--init'

        # Rebase submodule (2015.8.0 and later)
        salt myminion git.submodule /path/to/repo/sub/repo update opts='--rebase'

        # Add submodule (2015.8.0 and later)
        salt myminion git.submodule /path/to/repo/sub/repo add opts='https://mydomain.tld/repo.git'

        # Unregister submodule (2015.8.0 and later)
        salt myminion git.submodule /path/to/repo/sub/repo deinit
    '''
    kwargs = salt.utils.clean_kwargs(**kwargs)
    init_ = kwargs.pop('init', False)
    if kwargs:
        salt.utils.invalid_kwargs(kwargs)

    cwd = _expand_path(cwd, user)
    if init_:
        raise SaltInvocationError(
            'The \'init\' argument is no longer supported. Either set '
            '\'command\' to \'init\', or include \'--init\' in the \'opts\' '
            'argument and set \'command\' to \'update\'.'
        )
    if not isinstance(command, six.string_types):
        command = str(command)
    cmd = ['git', 'submodule', command]
    cmd.extend(_format_opts(opts))
    return _git_run(cmd,
                    cwd=cwd,
                    runas=user,
                    identity=identity,
                    ignore_retcode=ignore_retcode)['stdout']


def symbolic_ref(cwd,
                 ref,
                 value=None,
                 opts='',
                 user=None,
                 ignore_retcode=False):
    '''
    .. versionadded:: 2015.8.0

    Interface to `git-symbolic-ref(1)`_

    cwd
        The path to the git checkout

    ref
        Symbolic ref to read/modify

    value
        If passed, then the symbolic ref will be set to this value and an empty
        string will be returned.

        If not passed, then the ref to which ``ref`` points will be returned,
        unless ``--delete`` is included in ``opts`` (in which case the symbolic
        ref will be deleted).

    opts
        Any additional options to add to the command line, in a single string

    user
        User under which to run the git command. By default, the command is run
        by the user under which the minion is running.

    ignore_retcode : False
        If ``True``, do not log an error to the minion log if the git command
        returns a nonzero exit status.

        .. versionadded:: 2015.8.0

    .. _`git-symbolic-ref(1)`: http://git-scm.com/docs/git-symbolic-ref


    CLI Examples:

    .. code-block:: bash

        # Get ref to which HEAD is pointing
        salt myminion git.symbolic_ref /path/to/repo HEAD
        # Set/overwrite symbolic ref 'FOO' to local branch 'foo'
        salt myminion git.symbolic_ref /path/to/repo FOO refs/heads/foo
        # Delete symbolic ref 'FOO'
        salt myminion git.symbolic_ref /path/to/repo FOO opts='--delete'
    '''
    cwd = _expand_path(cwd, user)
    command = ['git', 'symbolic-ref']
    opts = _format_opts(opts)
    if value is not None and any(x in opts for x in ('-d', '--delete')):
        raise SaltInvocationError(
            'Value cannot be set for symbolic ref if -d/--delete is included '
            'in opts'
        )
    command.extend(opts)
    command.append(ref)
    if value:
        command.extend(value)
    return _git_run(command,
                    cwd=cwd,
                    runas=user,
                    ignore_retcode=ignore_retcode)['stdout']


def version(versioninfo=False):
    '''
    .. versionadded:: 2015.8.0

    Returns the version of Git installed on the minion

    versioninfo : False
        If ``True``, return the version in a versioninfo list (e.g. ``[2, 5,
        0]``)


    CLI Example:

    .. code-block:: bash

        salt myminion git.version
    '''
    contextkey = 'git.version'
    contextkey_info = 'git.versioninfo'
    if contextkey not in __context__:
        try:
            version_ = _git_run(['git', '--version'])['stdout']
        except CommandExecutionError as exc:
            log.error(
                'Failed to obtain the git version (error follows):\n{0}'
                .format(exc)
            )
            version_ = 'unknown'
        try:
            __context__[contextkey] = version_.split()[-1]
        except IndexError:
            # Somehow git --version returned no stdout while not raising an
            # error. Should never happen but we should still account for this
            # possible edge case.
            log.error('Running \'git --version\' returned no stdout')
            __context__[contextkey] = 'unknown'
    if not versioninfo:
        return __context__[contextkey]
    if contextkey_info not in __context__:
        # Set ptr to the memory location of __context__[contextkey_info] to
        # prevent repeated dict lookups
        ptr = __context__.setdefault(contextkey_info, [])
        for part in __context__[contextkey].split('.'):
            try:
                ptr.append(int(part))
            except ValueError:
                ptr.append(part)
    return __context__[contextkey_info]


def worktree_add(cwd,
                 worktree_path,
                 ref=None,
                 reset_branch=None,
                 force=None,
                 detach=False,
                 opts='',
                 user=None,
                 ignore_retcode=False,
                 **kwargs):
    '''
    .. versionadded:: 2015.8.0

    Interface to `git-worktree(1)`_, adds a worktree

    cwd
        The path to the git checkout

    worktree_path
        Path to the new worktree. Can be either absolute, or relative to
        ``cwd``.

    branch
        Name of new branch to create. If omitted, will be set to the basename
        of the ``worktree_path``. For example, if the ``worktree_path`` is
        ``/foo/bar/baz``, then ``branch`` will be ``baz``.

    ref
        Name of the ref on which to base the new worktree. If omitted, then
        ``HEAD`` is use, and a new branch will be created, named for the
        basename of the ``worktree_path``. For example, if the
        ``worktree_path`` is ``/foo/bar/baz`` then a new branch ``baz`` will be
        created, and pointed at ``HEAD``.

    reset_branch : False
        If ``False``, then `git-worktree(1)`_ will fail to create the worktree
        if the targeted branch already exists. Set this argument to ``True`` to
        reset the targeted branch to point at ``ref``, and checkout the
        newly-reset branch into the new worktree.

    force : False
        By default, `git-worktree(1)`_ will not permit the same branch to be
        checked out in more than one worktree. Set this argument to ``True`` to
        override this.

    opts
        Any additional options to add to the command line, in a single string

        .. note::
            On the Salt CLI, if the opts are preceded with a dash, it is
            necessary to precede them with ``opts=`` to avoid causing errors
            with Salt's own argument parsing.

            All CLI options for adding worktrees as of Git 2.5.0 are already
            supported by this function as of Salt 2015.8.0, so using this
            argument is unnecessary unless new CLI arguments are added to
            `git-worktree(1)`_ and are not yet supported in Salt.

    user
        User under which to run the git command. By default, the command is run
        by the user under which the minion is running.

    ignore_retcode : False
        If ``True``, do not log an error to the minion log if the git command
        returns a nonzero exit status.

        .. versionadded:: 2015.8.0

    .. _`git-worktree(1)`: http://git-scm.com/docs/git-worktree


    CLI Examples:

    .. code-block:: bash

        salt myminion git.worktree_add /path/to/repo/main ../hotfix ref=origin/master
        salt myminion git.worktree_add /path/to/repo/main ../hotfix branch=hotfix21 ref=v2.1.9.3
    '''
    _check_worktree_support()
    kwargs = salt.utils.clean_kwargs(**kwargs)
    branch_ = kwargs.pop('branch', None)
    if kwargs:
        salt.utils.invalid_kwargs(kwargs)

    cwd = _expand_path(cwd, user)
    if branch_ and detach:
        raise SaltInvocationError(
            'Only one of \'branch\' and \'detach\' is allowed'
        )

    command = ['git', 'worktree', 'add']
    if detach:
        if force:
            log.warning(
                '\'force\' argument to git.worktree_add is ignored when '
                'detach=True'
            )
        command.append('--detach')
    else:
        if not branch_:
            branch_ = os.path.basename(worktree_path)
        command.extend(['-B' if reset_branch else '-b', branch_])
        if force:
            command.append('--force')
    command.extend(_format_opts(opts))
    if not isinstance(worktree_path, six.string_types):
        worktree_path = str(worktree_path)
    command.append(worktree_path)
    if ref:
        if not isinstance(ref, six.string_types):
            ref = str(ref)
        command.append(ref)
    # Checkout message goes to stderr
    return _git_run(command,
                    cwd=cwd,
                    runas=user,
                    ignore_retcode=ignore_retcode,
                    redirect_stderr=True)['stdout']


def worktree_prune(cwd,
                   dry_run=False,
                   verbose=True,
                   expire=None,
                   opts='',
                   user=None,
                   ignore_retcode=False):
    '''
    .. versionadded:: 2015.8.0

    Interface to `git-worktree(1)`_, prunes stale worktree administrative data
    from the gitdir

    cwd
        The path to the main git checkout or a linked worktree

    dry_run : False
        If ``True``, then this function will report what would have been
        pruned, but no changes will be made.

    verbose : True
        Report all changes made. Set to ``False`` to suppress this output.

    expire
        Only prune unused worktree data older than a specific period of time.
        The date format for this parameter is described in the documentation
        for the ``gc.pruneWorktreesExpire`` config param in the
        `git-config(1)`_ manpage.

    opts
        Any additional options to add to the command line, in a single string

        .. note::
            On the Salt CLI, if the opts are preceded with a dash, it is
            necessary to precede them with ``opts=`` to avoid causing errors
            with Salt's own argument parsing.

            All CLI options for pruning worktrees as of Git 2.5.0 are already
            supported by this function as of Salt 2015.8.0, so using this
            argument is unnecessary unless new CLI arguments are added to
            `git-worktree(1)`_ and are not yet supported in Salt.

    user
        User under which to run the git command. By default, the command is run
        by the user under which the minion is running.

    ignore_retcode : False
        If ``True``, do not log an error to the minion log if the git command
        returns a nonzero exit status.

        .. versionadded:: 2015.8.0

    .. _`git-worktree(1)`: http://git-scm.com/docs/git-worktree
    .. _`git-config(1)`: http://git-scm.com/docs/git-config/2.5.1


    CLI Examples:

    .. code-block:: bash

        salt myminion git.worktree_prune /path/to/repo
        salt myminion git.worktree_prune /path/to/repo dry_run=True
        salt myminion git.worktree_prune /path/to/repo expire=1.day.ago
    '''
    _check_worktree_support()
    cwd = _expand_path(cwd, user)
    command = ['git', 'worktree', 'prune']
    if dry_run:
        command.append('--dry-run')
    if verbose:
        command.append('--verbose')
    if expire:
        if not isinstance(expire, six.string_types):
            expire = str(expire)
        command.extend(['--expire', expire])
    command.extend(_format_opts(opts))
    return _git_run(command,
                    cwd=cwd,
                    runas=user,
                    ignore_retcode=ignore_retcode)['stdout']


def worktree_rm(cwd, user=None):
    '''
    .. versionadded:: 2015.8.0

    Recursively removes the worktree located at ``cwd``, returning ``True`` if
    successful. This function will attempt to determine if ``cwd`` is actually
    a worktree by invoking :py:func:`git.is_worktree
    <salt.modules.git.is_worktree>`. If the path does not correspond to a
    worktree, then an error will be raised and no action will be taken.

    .. warning::

        There is no undoing this action. Be **VERY** careful before running
        this function.

    cwd
        Path to the worktree to be removed

    user
        User under which to run the git command. By default, the command is run
        by the user under which the minion is running.


    CLI Examples:

    .. code-block:: bash

        salt myminion git.worktree_rm /path/to/worktree
    '''
    _check_worktree_support()
    cwd = _expand_path(cwd, user)
    if not os.path.exists(cwd):
        raise CommandExecutionError(cwd + ' does not exist')
    elif not is_worktree(cwd):
        raise CommandExecutionError(cwd + ' is not a git worktree')
    try:
        salt.utils.rm_rf(cwd)
    except Exception as exc:
        raise CommandExecutionError(
            'Unable to remove {0}: {1}'.format(cwd, exc)
        )
    return True
