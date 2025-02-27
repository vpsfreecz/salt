# -*- coding: utf-8 -*-
'''
Manage X509 certificates

.. versionadded:: 2015.8.0

'''

# Import python libs
from __future__ import absolute_import
import os
import logging
import hashlib
import glob
import random
import ctypes
import tempfile
import yaml
import re
import datetime
import ast

# Import salt libs
import salt.utils
import salt.exceptions
import salt.ext.six as six
from salt.utils.odict import OrderedDict
from salt.ext.six.moves import range  # pylint: disable=import-error,redefined-builtin
from salt.state import STATE_INTERNAL_KEYWORDS as _STATE_INTERNAL_KEYWORDS

# Import 3rd Party Libs
try:
    import M2Crypto
    HAS_M2 = True
except ImportError:
    HAS_M2 = False

__virtualname__ = 'x509'

log = logging.getLogger(__name__)

EXT_NAME_MAPPINGS = OrderedDict([
                         ('basicConstraints', 'X509v3 Basic Constraints'),
                         ('keyUsage', 'X509v3 Key Usage'),
                         ('extendedKeyUsage', 'X509v3 Extended Key Usage'),
                         ('subjectKeyIdentifier', 'X509v3 Subject Key Identifier'),
                         ('authorityKeyIdentifier', 'X509v3 Authority Key Identifier'),
                         ('issuserAltName', 'X509v3 Issuer Alternative Name'),
                         ('authorityInfoAccess', 'X509v3 Authority Info Access'),
                         ('subjectAltName', 'X509v3 Subject Alternative Name'),
                         ('crlDistributionPoints', 'X509v3 CRL Distribution Points'),
                         ('issuingDistributionPoint', 'X509v3 Issuing Distribution Point'),
                         ('certificatePolicies', 'X509v3 Certificate Policies'),
                         ('policyConstraints', 'X509v3 Policy Constraints'),
                         ('inhibitAnyPolicy', 'X509v3 Inhibit Any Policy'),
                         ('nameConstraints', 'X509v3 Name Constraints'),
                         ('noCheck', 'X509v3 OCSP No Check'),
                         ('nsComment', 'Netscape Comment'),
                         ('nsCertType', 'Netscape Certificate Type'),
                    ])

CERT_DEFAULTS = {'days_valid': 365, 'version': 3, 'serial_bits': 64, 'algorithm': 'sha256'}


def __virtual__():
    '''
    only load this module if m2crypto is available
    '''
    if HAS_M2:
        return __virtualname__
    else:
        return (False, 'Could not load x509 module, m2crypto unavailable')


class _Ctx(ctypes.Structure):
    '''
    This is part of an ugly hack to fix an ancient bug in M2Crypto
    https://bugzilla.osafoundation.org/show_bug.cgi?id=7530#c13
    '''
    # pylint: disable=too-few-public-methods
    _fields_ = [('flags', ctypes.c_int),
                ('issuer_cert', ctypes.c_void_p),
                ('subject_cert', ctypes.c_void_p),
                ('subject_req', ctypes.c_void_p),
                ('crl', ctypes.c_void_p),
                ('db_meth', ctypes.c_void_p),
                ('db', ctypes.c_void_p),
                ]


def _fix_ctx(m2_ctx, issuer=None):
    '''
    This is part of an ugly hack to fix an ancient bug in M2Crypto
    https://bugzilla.osafoundation.org/show_bug.cgi?id=7530#c13
    '''
    ctx = _Ctx.from_address(int(m2_ctx))  # pylint: disable=no-member

    ctx.flags = 0
    ctx.subject_cert = None
    ctx.subject_req = None
    ctx.crl = None
    if issuer is None:
        ctx.issuer_cert = None
    else:
        ctx.issuer_cert = int(issuer.x509)


def _new_extension(name, value, critical=0, issuer=None, _pyfree=1):
    '''
    Create new X509_Extension, This is required because M2Crypto doesn't support
    getting the publickeyidentifier from the issuer to create the authoritykeyidentifier
    extension.
    '''
    if name == 'subjectKeyIdentifier' and \
        value.strip('0123456789abcdefABCDEF:') is not '':
        raise salt.exceptions.SaltInvocationError('value must be precomputed hash')

    lhash = M2Crypto.m2.x509v3_lhash()                      # pylint: disable=no-member
    ctx = M2Crypto.m2.x509v3_set_conf_lhash(lhash)          # pylint: disable=no-member
    #ctx not zeroed
    _fix_ctx(ctx, issuer)

    x509_ext_ptr = M2Crypto.m2.x509v3_ext_conf(lhash, ctx, name, value)  # pylint: disable=no-member
    #ctx,lhash freed

    if x509_ext_ptr is None:
        raise Exception
    x509_ext = M2Crypto.X509.X509_Extension(x509_ext_ptr, _pyfree)
    x509_ext.set_critical(critical)
    return x509_ext


# The next four functions are more hacks because M2Crypto doesn't support getting
# Extensions from CSRs. https://github.com/martinpaljak/M2Crypto/issues/63
def _parse_openssl_req(csr_filename):
    '''
    Parses openssl command line output, this is a workaround for M2Crypto's
    inability to get them from CSR objects.
    '''
    cmd = ('openssl req -text -noout -in {0}'.format(csr_filename))

    output = __salt__['cmd.run_stderr'](cmd)

    output = re.sub(r': rsaEncryption', ':', output)
    output = re.sub(r'[0-9a-f]{2}:', '', output)

    return yaml.safe_load(output)


def _get_csr_extensions(csr):
    '''
    Returns a list of dicts containing the name, value and critical value of
    any extension contained in a csr object.
    '''
    ret = OrderedDict()

    csrtempfile = tempfile.NamedTemporaryFile()
    csrtempfile.write(csr.as_pem())
    csrtempfile.flush()
    csryaml = _parse_openssl_req(csrtempfile.name)
    csrtempfile.close()
    try:
        csrexts = csryaml['Certificate Request']['Data']['Requested Extensions']
    except TypeError:
        csrexts = {}

    for short_name, long_name in six.iteritems(EXT_NAME_MAPPINGS):
        if long_name in csrexts:
            ret[short_name] = csrexts[long_name]

    return ret


# None of python libraries read CRLs. Again have to hack it with the openssl CLI
def _parse_openssl_crl(crl_filename):
    '''
    Parses openssl command line output, this is a workaround for M2Crypto's
    inability to get them from CSR objects.
    '''
    cmd = ('openssl crl -text -noout -in {0}'.format(crl_filename))

    output = __salt__['cmd.run_stderr'](cmd)

    crl = {}
    for line in output.split('\n'):
        line = line.strip()
        if line.startswith('Version '):
            crl['Version'] = line.replace('Version ', '')
        if line.startswith('Signature Algorithm: '):
            crl['Signature Algorithm'] = line.replace('Signature Algorithm: ', '')
        if line.startswith('Issuer: '):
            line = line.replace('Issuer: ', '')
            subject = {}
            for sub_entry in line.split('/'):
                if '=' in sub_entry:
                    sub_entry = sub_entry.split('=')
                    subject[sub_entry[0]] = sub_entry[1]
            crl['Issuer'] = subject
        if line.startswith('Last Update: '):
            crl['Last Update'] = line.replace('Last Update: ', '')
            last_update = datetime.datetime.strptime(
                    crl['Last Update'], "%b %d %H:%M:%S %Y %Z")
            crl['Last Update'] = last_update.strftime("%Y-%m-%d %H:%M:%S")
        if line.startswith('Next Update: '):
            crl['Next Update'] = line.replace('Next Update: ', '')
            next_update = datetime.datetime.strptime(
                    crl['Next Update'], "%b %d %H:%M:%S %Y %Z")
            crl['Next Update'] = next_update.strftime("%Y-%m-%d %H:%M:%S")
        if line.startswith('Revoked Certificates:'):
            break

    if 'No Revoked Certificates.' in output:
        crl['Revoked Certificates'] = []
        return crl

    output = output.split('Revoked Certificates:')[1]
    output = output.split('Signature Algorithm:')[0]

    rev = []
    for revoked in output.split('Serial Number: '):
        if not revoked.strip():
            continue

        rev_sn = revoked.split('\n')[0].strip()
        revoked = rev_sn + ':\n' + '\n'.join(revoked.split('\n')[1:])
        rev_yaml = yaml.safe_load(revoked)
        for rev_item, rev_values in six.iteritems(rev_yaml):               # pylint: disable=unused-variable
            if 'Revocation Date' in rev_values:
                rev_date = datetime.datetime.strptime(
                        rev_values['Revocation Date'], "%b %d %H:%M:%S %Y %Z")
                rev_values['Revocation Date'] = rev_date.strftime("%Y-%m-%d %H:%M:%S")

        rev.append(rev_yaml)

    crl['Revoked Certificates'] = rev

    return crl


def _get_signing_policy(name):
    policies = __salt__['pillar.get']('x509_signing_policies', None)
    if policies:
        signing_policy = policies.get(name)
        if signing_policy:
            return signing_policy
    return __salt__['config.get']('x509_signing_policies', {}).get(name)


def _pretty_hex(hex_str):
    '''
    Nicely formats hex strings
    '''
    if len(hex_str) % 2 != 0:
        hex_str = '0' + hex_str
    return ':'.join([hex_str[i:i+2] for i in range(0, len(hex_str), 2)]).upper()


def _dec2hex(decval):
    '''
    Converts decimal values to nicely formatted hex strings
    '''
    return _pretty_hex('{0:X}'.format(decval))


def _text_or_file(input_):
    '''
    Determines if input is a path to a file, or a string with the content to be parsed.
    '''
    if os.path.isfile(input_):
        with salt.utils.fopen(input_) as fp_:
            return fp_.read()
    else:
        return input_


def _parse_subject(subject):
    '''
    Returns a dict containing all values in an X509 Subject
    '''
    ret = {}
    nids = []
    for nid_name, nid_num in six.iteritems(subject.nid):
        if nid_num in nids:
            continue
        val = getattr(subject, nid_name)
        if val:
            ret[nid_name] = val
            nids.append(nid_num)

    return ret


def _get_certificate_obj(cert):
    '''
    Returns a certificate object based on PEM text.
    '''
    if isinstance(cert, M2Crypto.X509.X509):
        return cert

    text = _text_or_file(cert)
    text = get_pem_entry(text, pem_type='CERTIFICATE')
    return M2Crypto.X509.load_cert_string(text)


def _get_private_key_obj(private_key):
    '''
    Returns a private key object based on PEM text.
    '''
    private_key = _text_or_file(private_key)
    private_key = get_pem_entry(private_key)
    rsaprivkey = M2Crypto.RSA.load_key_string(private_key)
    evpprivkey = M2Crypto.EVP.PKey()
    evpprivkey.assign_rsa(rsaprivkey)
    return evpprivkey


def _get_request_obj(csr):
    '''
    Returns a CSR object based on PEM text.
    '''
    text = _text_or_file(csr)
    text = get_pem_entry(text, pem_type='CERTIFICATE REQUEST')
    return M2Crypto.X509.load_request_string(text)


def _get_pubkey_hash(cert):
    '''
    Returns the sha1 hash of the modulus of a public key in a cert
    Used for generating subject key identifiers
    '''
    sha_hash = hashlib.sha1(cert.get_pubkey().get_modulus()).hexdigest()
    return _pretty_hex(sha_hash)


def get_pem_entry(text, pem_type=None):
    '''
    Returns a properly formatted PEM string from the input text fixing
    any whitespace or line-break issues

    text:
        Text containing the X509 PEM entry to be returned or path to a file containing the text.

    pem_type:
        If specified, this function will only return a pem of a certain type, for example
        'CERTIFICATE' or 'CERTIFICATE REQUEST'.

    CLI Example:

    .. code-block:: bash

        salt '*' x509.get_pem_entry "-----BEGIN CERTIFICATE REQUEST-----MIICyzCC Ar8CAQI...-----END CERTIFICATE REQUEST"
    '''
    text = _text_or_file(text)

    if not pem_type:
        # Split based on headers
        if len(text.split('-----')) is not 5:
            raise salt.exceptions.SaltInvocationError('PEM text not valid:\n{0}'.format(text))
        pem_header = '-----'+text.split('-----')[1]+'-----'
        # Remove all whitespace from body
        pem_footer = '-----'+text.split('-----')[3]+'-----'
    else:
        pem_header = '-----BEGIN {0}-----'.format(pem_type)
        pem_footer = '-----END {0}-----'.format(pem_type)
        # Split based on defined headers
        if (len(text.split(pem_header)) is not 2 or
                len(text.split(pem_footer)) is not 2):
            raise salt.exceptions.SaltInvocationError(
                    'PEM does not contain a single entry of type {0}:\n'
                    '{1}'.format(pem_type, text))

    pem_body = text.split(pem_header)[1].split(pem_footer)[0]

    # Remove all whitespace from body
    pem_body = ''.join(pem_body.split())

    # Generate correctly formatted pem
    ret = pem_header+'\n'
    for i in range(0, len(pem_body), 64):
        ret += pem_body[i:i+64]+'\n'
    ret += pem_footer+'\n'

    return ret


def get_pem_entries(glob_path):
    '''
    Returns a dict containing PEM entries in files matching a glob

    glob_path:
        A path to certificates to be read and returned.

    CLI Example:

    .. code-block:: bash

        salt '*' x509.read_pem_entries "/etc/pki/*.crt"
    '''
    ret = {}

    for path in glob.glob(glob_path):
        if os.path.isfile(path):
            try:
                ret[path] = get_pem_entry(text=path)
            except ValueError:
                pass

    return ret


def read_certificate(certificate):
    '''
    Returns a dict containing details of a certificate. Input can be a PEM string or file path.

    certificate:
        The certificate to be read. Can be a path to a certificate file, or a string containing
        the PEM formatted text of the certificate.

    CLI Example:

    .. code-block:: bash

        salt '*' x509.read_certificate /etc/pki/mycert.crt
    '''
    cert = _get_certificate_obj(certificate)

    ret = {
        # X509 Version 3 has a value of 2 in the field.
        # Version 2 has a value of 1.
        # https://tools.ietf.org/html/rfc5280#section-4.1.2.1
        'Version': cert.get_version()+1,
        # Get size returns in bytes. The world thinks of key sizes in bits.
        'Key Size': cert.get_pubkey().size()*8,
        'Serial Number': _dec2hex(cert.get_serial_number()),
        'SHA-256 Finger Print': _pretty_hex(cert.get_fingerprint(md='sha256')),
        'MD5 Finger Print': _pretty_hex(cert.get_fingerprint(md='md5')),
        'SHA1 Finger Print': _pretty_hex(cert.get_fingerprint(md='sha1')),
        'Subject': _parse_subject(cert.get_subject()),
        'Subject Hash': _dec2hex(cert.get_subject().as_hash()),
        'Issuer': _parse_subject(cert.get_issuer()),
        'Issuer Hash': _dec2hex(cert.get_issuer().as_hash()),
        'Not Before': cert.get_not_before().get_datetime().strftime('%Y-%m-%d %H:%M:%S'),
        'Not After': cert.get_not_after().get_datetime().strftime('%Y-%m-%d %H:%M:%S'),
        'Public Key': get_public_key(cert)
    }

    exts = OrderedDict()
    for ext_index in range(0, cert.get_ext_count()):
        ext = cert.get_ext_at(ext_index)
        name = ext.get_name()
        val = ext.get_value()
        if ext.get_critical():
            val = 'critical ' + val
        exts[name] = val

    if exts:
        ret['X509v3 Extensions'] = exts

    return ret


def read_certificates(glob_path):
    '''
    Returns a dict containing details of a all certificates matching a glob

    glob_path:
        A path to certificates to be read and returned.

    CLI Example:

    .. code-block:: bash

        salt '*' x509.read_certificates "/etc/pki/*.crt"
    '''
    ret = {}

    for path in glob.glob(glob_path):
        if os.path.isfile(path):
            try:
                ret[path] = read_certificate(certificate=path)
            except ValueError:
                pass

    return ret


def read_csr(csr):
    '''
    Returns a dict containing details of a certificate request.

    :depends:   - OpenSSL command line tool

    csr:
        A path or PEM encoded string containing the CSR to read.

    CLI Example:

    .. code-block:: bash

        salt '*' x509.read_csr /etc/pki/mycert.csr
    '''
    csr = _get_request_obj(csr)
    ret = {
           # X509 Version 3 has a value of 2 in the field.
           # Version 2 has a value of 1.
           # https://tools.ietf.org/html/rfc5280#section-4.1.2.1
           'Version': csr.get_version()+1,
           # Get size returns in bytes. The world thinks of key sizes in bits.
           'Subject': _parse_subject(csr.get_subject()),
           'Subject Hash': _dec2hex(csr.get_subject().as_hash()),
           }

    ret['X509v3 Extensions'] = _get_csr_extensions(csr)

    return ret


def read_crl(crl):
    '''
    Returns a dict containing details of a certificate revocation list. Input can be a PEM string or file path.

    :depends:   - OpenSSL command line tool

    csl:
        A path or PEM encoded string containing the CSL to read.

    CLI Example:

    .. code-block:: bash

        salt '*' x509.read_crl /etc/pki/mycrl.crl
    '''
    text = _text_or_file(crl)
    text = get_pem_entry(text, pem_type='X509 CRL')

    crltempfile = tempfile.NamedTemporaryFile()
    crltempfile.write(text)
    crltempfile.flush()
    crlparsed = _parse_openssl_crl(crltempfile.name)
    crltempfile.close()

    return crlparsed


def get_public_key(key, asObj=False):
    '''
    Returns a string containing the public key in PEM format.

    key:
        A path or PEM encoded string containing a CSR, Certificate or Private Key from which
        a public key can be retrieved.

    CLI Example:

    .. code-block:: bash

        salt '*' x509.get_public_key /etc/pki/mycert.cer
    '''

    if isinstance(key, M2Crypto.X509.X509):
        rsa = key.get_pubkey().get_rsa()
        text = ''
    else:
        text = _text_or_file(key)
        text = get_pem_entry(text)

    if text.startswith('-----BEGIN PUBLIC KEY-----'):
        if not asObj:
            return text
        bio = M2Crypto.BIO.MemoryBuffer()
        bio.write(text)
        rsa = M2Crypto.RSA.load_pub_key_bio(bio)

    bio = M2Crypto.BIO.MemoryBuffer()
    if text.startswith('-----BEGIN CERTIFICATE-----'):
        cert = M2Crypto.X509.load_cert_string(text)
        rsa = cert.get_pubkey().get_rsa()
    if text.startswith('-----BEGIN CERTIFICATE REQUEST-----'):
        csr = M2Crypto.X509.load_request_string(text)
        rsa = csr.get_pubkey().get_rsa()
    if (text.startswith('-----BEGIN PRIVATE KEY-----') or
            text.startswith('-----BEGIN RSA PRIVATE KEY-----')):
        rsa = M2Crypto.RSA.load_key_string(text)

    if asObj:
        evppubkey = M2Crypto.EVP.PKey()
        evppubkey.assign_rsa(rsa)
        return evppubkey

    rsa.save_pub_key_bio(bio)
    return bio.read_all()


def get_private_key_size(private_key):
    '''
    Returns the bit length of a private key in PEM format.

    private_key:
        A path or PEM encoded string containing a private key.

    CLI Example:

    .. code-block:: bash

        salt '*' x509.get_private_key_size /etc/pki/mycert.key
    '''
    return _get_private_key_obj(private_key).size()*8


def write_pem(text, path, pem_type=None):
    '''
    Writes out a PEM string fixing any formatting or whitespace issues before writing.

    text:
        PEM string input to be written out.

    path:
        Path of the file to write the pem out to.

    pem_type:
        The PEM type to be saved, for example ``CERTIFICATE`` or ``PUBLIC KEY``. Adding this
        will allow the function to take input that may contain multiple pem types.

    CLI Example:

    .. code-block:: bash

        salt '*' x509.write_pem "-----BEGIN CERTIFICATE-----MIIGMzCCBBugA..." path=/etc/pki/mycert.crt
    '''
    text = get_pem_entry(text, pem_type=pem_type)
    with salt.utils.fopen(path, 'w') as fp_:
        fp_.write(text)
    return 'PEM written to {0}'.format(path)


def create_private_key(path=None, text=False, bits=2048):
    '''
    Creates a private key in PEM format.

    path:
        The path to write the file to, either ``path`` or ``text`` are required.

    text:
        If ``True``, return the PEM text without writing to a file. Default ``False``.

    bits:
        Length of the private key in bits. Default 2048

    CLI Example:

    .. code-block:: bash

        salt '*' x509.create_private_key path=/etc/pki/mykey.key
    '''
    if not path and not text:
        raise salt.exceptions.SaltInvocationError('Either path or text must be specified.')
    if path and text:
        raise salt.exceptions.SaltInvocationError('Either path or text must be specified, not both.')

    rsa = M2Crypto.RSA.gen_key(bits, M2Crypto.m2.RSA_F4)            # pylint: disable=no-member
    bio = M2Crypto.BIO.MemoryBuffer()
    rsa.save_key_bio(bio, cipher=None)

    if path:
        return write_pem(text=bio.read_all(), path=path,
                pem_type='RSA PRIVATE KEY')
    else:
        return bio.read_all()


def create_crl(path=None, text=False, signing_private_key=None,
        signing_cert=None, revoked=None, include_expired=False,
        days_valid=100):
    '''
    Create a CRL

    :depends:   - PyOpenSSL Python module

    path:
        Path to write the crl to.

    text:
        If ``True``, return the PEM text without writing to a file. Default ``False``.

    signing_private_key:
        A path or string of the private key in PEM format that will be used to sign this crl.
        This is required.

    signing_cert:
        A certificate matching the private key that will be used to sign this crl. This is
        required.

    revoked:
        A list of dicts containing all the certificates to revoke. Each dict represents one
        certificate. A dict must contain either the key ``serial_number`` with the value of
        the serial number to revoke, or ``certificate`` with either the PEM encoded text of
        the certificate, or a path ot the certificate to revoke.

        The dict can optionally contain the ``revocation_date`` key. If this key is omitted
        the revocation date will be set to now. If should be a string in the format "%Y-%m-%d %H:%M:%S".

        The dict can also optionally contain the ``not_after`` key. This is redundant if the
        ``certificate`` key is included. If the ``Certificate`` key is not included, this
        can be used for the logic behind the ``include_expired`` parameter.
        If should be a string in the format "%Y-%m-%d %H:%M:%S".

        The dict can also optionally contain the ``reason`` key. This is the reason code for the
        revocation. Available choices are ``unspecified``, ``keyCompromise``, ``CACompromise``,
        ``affiliationChanged``, ``superseded``, ``cessationOfOperation`` and ``certificateHold``.

    include_expired:
        Include expired certificates in the CRL. Default is ``False``.

    days_valid:
        The number of days that the CRL should be valid. This sets the Next Update field in the CRL.

    .. note

        At this time the pyOpenSSL library does not allow choosing a signing algorithm for CRLs
        See https://github.com/pyca/pyopenssl/issues/159

    CLI Example:

    .. code-block:: bash

        salt '*' x509.create_crl path=/etc/pki/mykey.key signing_private_key=/etc/pki/ca.key \\
                signing_cert=/etc/pki/ca.crt \\
                revoked="{'compromized-web-key': {'certificate': '/etc/pki/certs/www1.crt', \\
                'revocation_date': '2015-03-01 00:00:00'}}"
    '''
    # pyOpenSSL is required for dealing with CSLs. Importing inside these functions because
    # Client operations like creating CRLs shouldn't require pyOpenSSL
    # Note due to current limitations in pyOpenSSL it is impossible to specify a digest
    # For signing the CRL. This will hopefully be fixed soon: https://github.com/pyca/pyopenssl/pull/161
    import OpenSSL
    crl = OpenSSL.crypto.CRL()

    if revoked is None:
        revoked = []

    for rev_item in revoked:
        if 'certificate' in rev_item:
            rev_cert = read_certificate(rev_item['certificate'])
            rev_item['serial_number'] = rev_cert['Serial Number']
            rev_item['not_after'] = rev_cert['Not After']

        serial_number = rev_item['serial_number'].replace(':', '')
        serial_number = str(int(serial_number, 16))

        if 'not_after' in rev_item and not include_expired:
            not_after = datetime.datetime.strptime(rev_item['not_after'], '%Y-%m-%d %H:%M:%S')
            if datetime.datetime.now() > not_after:
                continue

        if 'revocation_date' not in rev_item:
            rev_item['revocation_date'] = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')

        rev_date = datetime.datetime.strptime(rev_item['revocation_date'], '%Y-%m-%d %H:%M:%S')
        rev_date = rev_date.strftime('%Y%m%d%H%M%SZ')

        rev = OpenSSL.crypto.Revoked()
        rev.set_serial(serial_number)
        rev.set_rev_date(rev_date)

        if 'reason' in rev_item:
            rev.set_reason(rev_item['reason'])

        crl.add_revoked(rev)

    signing_cert = _text_or_file(signing_cert)
    cert = OpenSSL.crypto.load_certificate(OpenSSL.crypto.FILETYPE_PEM,
            get_pem_entry(signing_cert, pem_type='CERTIFICATE'))
    signing_private_key = _text_or_file(signing_private_key)
    key = OpenSSL.crypto.load_privatekey(OpenSSL.crypto.FILETYPE_PEM,
            get_pem_entry(signing_private_key))

    crltext = crl.export(cert, key, OpenSSL.crypto.FILETYPE_PEM, days=days_valid)

    if text:
        return crltext

    return write_pem(text=crltext, path=path,
                pem_type='X509 CRL')


def sign_remote_certificate(argdic, **kwargs):
    '''
    Request a certificate to be remotely signed according to a signing policy.

    argdic:
        A dict containing all the arguments to be passed into the create_certificate function.
        This will become kwargs when passed to create_certificate.

    kwargs:
        kwargs delivered from publish.publish

    CLI Example:

    .. code-block:: bash

        salt '*' x509.sign_remote_certificate argdic="{'public_key': '/etc/pki/www.key', \\
                'signing_policy': 'www'}" __pub_id='www1'
    '''
    if 'signing_policy' not in argdic:
        return 'signing_policy must be specified'

    if not isinstance(argdic, dict):
        argdic = ast.literal_eval(argdic)

    signing_policy = {}
    if 'signing_policy' in argdic:
        signing_policy = _get_signing_policy(argdic['signing_policy'])
        if not signing_policy:
            return 'Signing policy {0} does not exist.'.format(argdic['signing_policy'])

        if isinstance(signing_policy, list):
            dict_ = {}
            for item in signing_policy:
                dict_.update(item)
            signing_policy = dict_

    if 'minions' in signing_policy:
        if '__pub_id' not in kwargs:
            return 'minion sending this request could not be identified'
        if not __salt__['match.glob'](signing_policy['minions'], kwargs['__pub_id']):
            return '{0} not permitted to use signing policy {1}'.format(kwargs['__pub_id'], argdic['signing_policy'])

    try:
        return create_certificate(path=None, text=True, **argdic)
    except Exception as except_:                                       # pylint: disable=broad-except
        return str(except_)


def get_signing_policy(signing_policy_name):
    '''
    Returns the details of a names signing policy, including the text of the public key that will be used
    to sign it. Does not return the private key.

    CLI Example:

    .. code-block:: bash

        salt '*' x509.get_signing_policy www
    '''
    signing_policy = _get_signing_policy(signing_policy_name)
    if not signing_policy:
        return 'Signing policy {0} does not exist.'.format(signing_policy_name)
    if isinstance(signing_policy, list):
        dict_ = {}
        for item in signing_policy:
            dict_.update(item)
        signing_policy = dict_

    try:
        del signing_policy['signing_private_key']
    except KeyError:
        pass

    try:
        signing_policy['signing_cert'] = get_pem_entry(signing_policy['signing_cert'], 'CERTIFICATE')
    except KeyError:
        pass

    return signing_policy


def create_certificate(path=None, text=False, ca_server=None, **kwargs):
    '''
    Create an X509 certificate.

    path:
        Path to write the certificate to.

    text:
        If ``True``, return the PEM text without writing to a file. Default ``False``.

    kwargs:
        Any of the properties below can be included as additional keyword arguments.

    ca_server:
        Request a remotely signed certificate from ca_server. For this to work, a ``signing_policy`` must
        be specified, and that same policy must be configured on the ca_server. See ``signing_policy`` for
        details. Also the salt master must permit peers to call the ``sign_remote_certificate`` function.

        Example:

        /etc/salt/master.d/peer.conf

        .. code-block:: yaml

            peer:
              .*:
                - x509.sign_remote_certificate

    subject properties:
        Any of the values below can be incldued to set subject properties
        Any other subject properties supported by OpenSSL should also work.

        C:
            2 letter Country code
        CN:
            Certificate common name, typically the FQDN.

        Email:
            Email address

        GN:
            Given Name

        L:
            Locality

        O:
            Organization

        OU:
            Organization Unit

        SN:
            SurName

        ST:
            State or Province

    signing_private_key:
        A path or string of the private key in PEM format that will be used to sign this certificate.
        If neither ``signing_cert``, ``public_key``, or ``csr`` are included, it will be assumed that
        this is a self-signed certificate, and the public key matching ``signing_private_key`` will
        be used to create the certificate.

    signing_cert:
        A certificate matching the private key that will be used to sign this certificate. This is used
        to populate the issuer values in the resulting certificate. Do not include this value for
        self-signed certificates.

    public_key:
        The public key to be included in this certificate. This can be sourced from a public key,
        certificate, csr or private key. If a private key is used, the matching public key from
        the private key will be generated before any processing is done. This means you can request a
        certificate from a remote CA using a private key file as your public_key and only the
        public key will be sent across the network to the CA.
        If neither ``public_key`` or ``csr`` are
        specified, it will be assumed that this is a self-signed certificate, and the public key
        derived from ``signing_private_key`` will be used. Specify either ``public_key`` or ``csr``,
        not both. Because you can input a CSR as a public key or as a CSR, it is important to understand
        the difference. If you import a CSR as a public key, only the public key will be added
        to the certificate, subject or extension information in the CSR will be lost.

    csr:
        A file or PEM string containing a certificate signing request. This will be used to supply the
        subject, extensions and public key of a certificate. Any subject or extensions specified
        explicitly will overwrite any in the CSR.

    basicConstraints:
        X509v3 Basic Constraints extension.

    extensions:
        The following arguments set X509v3 Extension values. If the value starts with ``critical ``,
        the extension will be marked as critical

        Some special extensions are ``subjectKeyIdentifier`` and ``authorityKeyIdentifier``.

        ``subjectKeyIdentifier`` can be an explicit value or it can be the special string ``hash``.
        ``hash`` will set the subjectKeyIdentifier equal to the SHA1 hash of the modulus of the
        public key in this certificate. Note that this is not the exact same hashing method used by
        OpenSSL when using the hash value.

        ``authorityKeyIdentifier`` Use values acceptable to the openssl CLI tools. This will
        automatically populate ``authorityKeyIdentifier`` with the ``subjectKeyIdentifier`` of
        ``signing_cert``. If this is a self-signed cert these values will be the same.

        basicConstraints:
            X509v3 Basic Constraints

        keyUsage:
            X509v3 Key Usage

        extendedKeyUsage:
            X509v3 Extended Key Usage

        subjectKeyIdentifier:
            X509v3 Subject Key Identifier

        issuerAltName:
            X509v3 Issuer Alternative Name

        subjectAltName:
            X509v3 Subject Alternative Name

        crlDistributionPoints:
            X509v3 CRL distribution points

        issuingDistributionPoint:
            X509v3 Issuing Distribution Point

        certificatePolicies:
            X509v3 Certificate Policies

        policyConstraints:
            X509v3 Policy Constraints

        inhibitAnyPolicy:
            X509v3 Inhibit Any Policy

        nameConstraints:
            X509v3 Name Constraints

        noCheck:
            X509v3 OCSP No Check

        nsComment:
            Netscape Comment

        nsCertType:
            Netscape Certificate Type

    days_valid:
        The number of days this certificate should be valid. This sets the ``notAfter`` property
        of the certificate. Defaults to 365.

    version:
        The version of the X509 certificate. Defaults to 3. This is automatically converted to the
        version value, so ``version=3`` sets the certificate version field to 0x2.

    serial_number:
        The serial number to assign to this certificate. If omitted a random serial number of size
        ``serial_bits`` is generated.

    serial_bits:
        The number of bits to use when randomly generating a serial number. Defaults to 64.

    algorithm:
        The hashing algorithm to be used for signing this certificate. Defaults to sha256.

    copypath:
        An additional path to copy the resulting certificate to. Can be used to maintain a copy
        of all certificates issued for revocation purposes.

    signing_policy:
        A signing policy that should be used to create this certificate. Signing policies should be defined
        in the minion configuration, or in a minion pillar. It should be a yaml formatted list of arguments
        which will override any arguments passed to this function. If the ``minions`` key is included in
        the signing policy, only minions matching that pattern will be permitted to remotely request certificates
        from that policy.

        Example:

        .. code-block:: yaml

            x509_signing_policies:
              www:
                - minions: 'www*'
                - signing_private_key: /etc/pki/ca.key
                - signing_cert: /etc/pki/ca.crt
                - C: US
                - ST: Utah
                - L: Salt Lake City
                - basicConstraints: "critical CA:false"
                - keyUsage: "critical cRLSign, keyCertSign"
                - subjectKeyIdentifier: hash
                - authorityKeyIdentifier: keyid,issuer:always
                - days_valid: 90
                - copypath: /etc/pki/issued_certs/

        The above signing policy can be invoked with ``signing_policy=www``

    CLI Example:

    .. code-block:: bash

        salt '*' x509.create_certificate path=/etc/pki/myca.crt \\
        signing_private_key='/etc/pki/myca.key' csr='/etc/pki/myca.csr'}
    '''

    if not path and not text and ('testrun' not in kwargs or kwargs['testrun'] is False):
        raise salt.exceptions.SaltInvocationError('Either path or text must be specified.')
    if path and text:
        raise salt.exceptions.SaltInvocationError('Either path or text must be specified, not both.')

    if ca_server:
        if 'signing_policy' not in kwargs:
            raise salt.exceptions.SaltInvocationError('signing_policy must be specified'
                    'if requesting remote certificate from ca_server {0}.'.format(ca_server))
        if 'csr' in kwargs:
            kwargs['csr'] = get_pem_entry(kwargs['csr'], pem_type='CERTIFICATE REQUEST').replace('\n', '')
        if 'public_key' in kwargs:
            # Strip newlines to make passing through as cli functions easier
            kwargs['public_key'] = get_public_key(kwargs['public_key']).replace('\n', '')

        # Remove system entries in kwargs
        # Including listen_in and preqreuired because they are not included in STATE_INTERNAL_KEYWORDS
        # for salt 2014.7.2
        for ignore in list(_STATE_INTERNAL_KEYWORDS) + ['listen_in', 'preqrequired']:
            kwargs.pop(ignore, None)

        cert_txt = __salt__['publish.publish'](tgt=ca_server,
                                               fun='x509.sign_remote_certificate',
                                               arg=str(kwargs))[ca_server]
        if path:
            return write_pem(text=cert_txt, path=path,
                    pem_type='CERTIFICATE')
        else:
            return cert_txt

    signing_policy = {}
    if 'signing_policy' in kwargs:
        signing_policy = _get_signing_policy(kwargs['signing_policy'])
        if isinstance(signing_policy, list):
            dict_ = {}
            for item in signing_policy:
                dict_.update(item)
            signing_policy = dict_

    # Overwrite any arguments in kwargs with signing_policy
    kwargs.update(signing_policy)

    for prop, default in six.iteritems(CERT_DEFAULTS):
        if prop not in kwargs:
            kwargs[prop] = default

    cert = M2Crypto.X509.X509()
    subject = cert.get_subject()

    # X509 Version 3 has a value of 2 in the field.
    # Version 2 has a value of 1.
    # https://tools.ietf.org/html/rfc5280#section-4.1.2.1
    cert.set_version(kwargs['version'] - 1)

    # Random serial number if not specified
    if 'serial_number' not in kwargs:
        kwargs['serial_number'] = _dec2hex(random.getrandbits(kwargs['serial_bits']))
    cert.set_serial_number(int(kwargs['serial_number'].replace(':', ''), 16))

    # Set validity dates
    # pylint: disable=no-member
    not_before = M2Crypto.m2.x509_get_not_before(cert.x509)
    not_after = M2Crypto.m2.x509_get_not_after(cert.x509)
    M2Crypto.m2.x509_gmtime_adj(not_before, 0)
    M2Crypto.m2.x509_gmtime_adj(not_after, 60*60*24*kwargs['days_valid'])
    # pylint: enable=no-member

    # If neither public_key or csr are included, this cert is self-signed
    if 'public_key' not in kwargs and 'csr' not in kwargs:
        kwargs['public_key'] = kwargs['signing_private_key']

    csrexts = {}
    if 'csr' in kwargs:
        kwargs['public_key'] = kwargs['csr']
        csr = _get_request_obj(kwargs['csr'])
        subject = csr.get_subject()
        csrexts = read_csr(kwargs['csr'])['X509v3 Extensions']

    cert.set_pubkey(get_public_key(kwargs['public_key'], asObj=True))

    for entry, num in six.iteritems(subject.nid):                  # pylint: disable=unused-variable
        if entry in kwargs:
            setattr(subject, entry, kwargs[entry])

    if 'signing_cert' in kwargs:
        signing_cert = _get_certificate_obj(kwargs['signing_cert'])
    else:
        signing_cert = cert
    cert.set_issuer(signing_cert.get_subject())

    for extname, extlongname in six.iteritems(EXT_NAME_MAPPINGS):
        if (extname in kwargs or extlongname in kwargs or extname in csrexts or extlongname in csrexts) is False:
            continue

        # Use explicitly set values first, fall back to CSR values.
        extval = kwargs[extname] or kwargs[extlongname] or csrexts[extname] or csrexts[extlongname]

        critical = False
        if extval.startswith('critical '):
            critical = True
            extval = extval[9:]

        if extname == 'subjectKeyIdentifier' and 'hash' in extval:
            extval = extval.replace('hash', _get_pubkey_hash(cert))

        issuer = None
        if extname == 'authorityKeyIdentifier':
            issuer = signing_cert

        ext = _new_extension(name=extname, value=extval, critical=critical, issuer=issuer)
        if not ext.x509_ext:
            log.info('Invalid X509v3 Extension. {0}: {1}'.format(extname, extval))
            continue

        cert.add_ext(ext)

    if 'testrun' in kwargs and kwargs['testrun'] is True:
        cert_props = read_certificate(cert)
        cert_props['Issuer Public Key'] = get_public_key(kwargs['signing_private_key'])
        return cert_props

    if not verify_private_key(kwargs['signing_private_key'], signing_cert):
        raise salt.exceptions.SaltInvocationError('signing_private_key: {0}'
                'does no match signing_cert: {1}'.format(kwargs['signing_private_key'],
                                                         kwargs['signing_cert']))

    cert.sign(_get_private_key_obj(kwargs['signing_private_key']), kwargs['algorithm'])

    if not verify_signature(cert, signing_pub_key=signing_cert):
        raise salt.exceptions.SaltInvocationError('failed to verify certificate signature')

    if 'copypath' in kwargs:
        write_pem(text=cert.as_pem(), path=os.path.join(kwargs['copypath'], kwargs['serial_number']+'.crt'),
                pem_type='CERTIFICATE')

    if path:
        return write_pem(text=cert.as_pem(), path=path,
                pem_type='CERTIFICATE')
    else:
        return cert.as_pem()


def create_csr(path=None, text=False, **kwargs):
    '''
    Create a certificate signing request.

    path:
        Path to write the certificate to.

    text:
        If ``True``, return the PEM text without writing to a file. Default ``False``.

    kwargs:
        The subject, extension and version arguments from
        :mod:`x509.create_certificate <salt.modules.x509.create_certificate>` can be used.

    CLI Example:

    .. code-block:: bash

        salt '*' x509.create_csr path=/etc/pki/myca.csr public_key='/etc/pki/myca.key' CN='My Cert
    '''

    if not path and not text:
        raise salt.exceptions.SaltInvocationError('Either path or text must be specified.')
    if path and text:
        raise salt.exceptions.SaltInvocationError('Either path or text must be specified, not both.')

    csr = M2Crypto.X509.Request()
    subject = csr.get_subject()
    csr.set_version(kwargs['version'] - 1)

    if 'public_key' not in kwargs:
        raise salt.exceptions.SaltInvocationError('public_key is required')
    csr.set_pubkey(get_public_key(kwargs['public_key'], asObj=True))

    for entry, num in six.iteritems(subject.nid):                  # pylint: disable=unused-variable
        if entry in kwargs:
            setattr(subject, entry, kwargs[entry])

    extstack = M2Crypto.X509.X509_Extension_Stack()
    for extname, extlongname in six.iteritems(EXT_NAME_MAPPINGS):
        if extname not in kwargs or extlongname not in kwargs:
            continue

        extval = kwargs[extname] or kwargs[extlongname]

        critical = False
        if extval.startswith('critical '):
            critical = True
            extval = extval[9:]

        issuer = None
        ext = _new_extension(name=extname, value=extval, critical=critical, issuer=issuer)
        if not ext.x509_ext:
            log.info('Invalid X509v3 Extension. {0}: {1}'.format(extname, extval))
            continue

        extstack.push(ext)

    csr.add_extensions(extstack)

    if path:
        return write_pem(text=csr.as_pem(), path=path,
                pem_type='CERTIFICATE REQUEST')
    else:
        return csr.as_pem()


def verify_private_key(private_key, public_key):
    '''
    Verify that 'private_key' matches 'public_key'

    private_key:
        The private key to verify, can be a string or path to a private key in PEM format.

    public_key:
        The public key to verify, can be a string or path to a PEM formatted certificate, csr,
        or another private key.

    CLI Example:

    .. code-block:: bash

        salt '*' x509.verify_private_key private_key=/etc/pki/myca.key public_key=/etc/pki/myca.crt
    '''
    return bool(get_public_key(private_key) == get_public_key(public_key))


def verify_signature(certificate, signing_pub_key=None):
    '''
    Verify that ``certificate`` has been signed by ``signing_pub_key``

    certificate:
        The certificate to verify. Can be a path or string containing a PEM formatted certificate.

    signing_pub_key:
        The public key to verify, can be a string or path to a PEM formatted certificate, csr,
        or private key.

    CLI Example:

    .. code-block:: bash

        salt '*' x509.verify_private_key private_key=/etc/pki/myca.key public_key=/etc/pki/myca.crt
    '''
    cert = _get_certificate_obj(certificate)

    if signing_pub_key:
        signing_pub_key = get_public_key(signing_pub_key, asObj=True)

    return bool(cert.verify(pkey=signing_pub_key) == 1)


def verify_crl(crl, cert):
    '''
    Validate a CRL against a certificate.
    Parses openssl command line output, this is a workaround for M2Crypto's
    inability to get them from CSR objects.

    crl:
        The CRL to verify

    cert:
        The certificate to verify the CRL against

    CLI Example:

    .. code-block:: bash

        salt '*' x509.verify_crl crl=/etc/pki/myca.crl cert=/etc/pki/myca.crt
    '''
    crltext = _text_or_file(crl)
    crltext = get_pem_entry(crltext, pem_type='X509 CRL')
    crltempfile = tempfile.NamedTemporaryFile()
    crltempfile.write(crltext)
    crltempfile.flush()

    certtext = _text_or_file(cert)
    certtext = get_pem_entry(certtext, pem_type='CERTIFICATE')
    certtempfile = tempfile.NamedTemporaryFile()
    certtempfile.write(certtext)
    certtempfile.flush()

    cmd = ('openssl crl -noout -in {0} -CAfile {1}'.format(crltempfile.name, certtempfile.name))

    output = __salt__['cmd.run_stderr'](cmd)

    crltempfile.close()
    certtempfile.close()

    if 'verify OK' in output:
        return True
    else:
        return False
