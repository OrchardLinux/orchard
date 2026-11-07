"""Bounded boot identity parameters shared by payloads and RAM recovery."""
import base64
import binascii
import json
import re

POLICY_PARAMETER = 'orchard.recovery-policy'
HANDOFF_PARAMETER = 'orchard.recovery-handoff'
MAX_ENCODED_BYTES = 2048
MAX_CMDLINE_BYTES = 1023
POLICY_FIELDS = frozenset(('schema', 'source', 'profile', 'compatibles', 'chip', 'board'))
POLICY_SOURCES_FIELDS = POLICY_FIELDS - {'source'} | {'sources'}
SOURCES = frozenset(('host-usb', 'ios-device'))
HANDOFF_FIELDS = frozenset(('schema', 'source', 'profile', 'ecid', 'chip', 'board',
                            'handoff_id', 'template_sha256'))


def _pairs(items):
    result = {}
    for key, value in items:
        if key in result:
            raise ValueError('Identity JSON repeats a field')
        result[key] = value
    return result


def object_json(raw):
    """Decode one bounded JSON object with no duplicate keys or NUL bytes."""
    if not isinstance(raw, bytes) or len(raw) > MAX_ENCODED_BYTES or b'\0' in raw:
        raise ValueError('Identity JSON exceeds its bound or contains a NUL')
    try:
        result = json.loads(raw.decode('utf-8'), object_pairs_hook=_pairs)
    except (UnicodeError, json.JSONDecodeError) as error:
        raise ValueError('Invalid identity JSON') from error
    if not isinstance(result, dict):
        raise ValueError('Identity JSON must be an object')
    return result


def _selected(result, fields, schema=1):
    if (not isinstance(result, dict) or set(result) != fields or
            type(result.get('schema')) is not int or result['schema'] != schema or
            not isinstance(result.get('profile'), str) or
            not re.fullmatch(r'[a-z0-9][a-z0-9._-]*', result['profile'])):
        raise ValueError('Unsupported recovery identity fields')
    if (type(result.get('chip')) is not int or not 0 < result['chip'] <= 0xffffffff or
            type(result.get('board')) is not int or not 0 <= result['board'] <= 0xffffffff):
        raise ValueError('Recovery identity has invalid chip identity')


def validate_policy(result):
    if isinstance(result, dict) and result.get('schema') == 2:
        _selected(result, POLICY_SOURCES_FIELDS, 2)
        sources = result['sources']
        if (not isinstance(sources, list) or not 1 <= len(sources) <= len(SOURCES) or
                any(not isinstance(value, str) or value not in SOURCES for value in sources) or
                len(set(sources)) != len(sources)):
            raise ValueError('Recovery identity policy has invalid sources')
    else:
        _selected(result, POLICY_FIELDS)
        if result['source'] != 'host-usb':
            raise ValueError('Schema-1 recovery identity policy requires host-usb')
    compatibles = result.get('compatibles')
    if (not isinstance(compatibles, list) or not 1 <= len(compatibles) <= 32 or
            any(not isinstance(value, str) or not re.fullmatch(r'apple,[a-zA-Z0-9._+-]{1,120}', value)
                for value in compatibles) or len(set(compatibles)) != len(compatibles)):
        raise ValueError('Recovery identity policy has invalid compatibles')
    return result


def allows_source(policy, source):
    """An origin is accepted only when the staged descriptor permits it."""
    validate_policy(policy)
    return source in (policy['sources'] if policy['schema'] == 2 else [policy['source']])


def validate_handoff(result):
    _selected(result, HANDOFF_FIELDS)
    if not isinstance(result['source'], str) or result['source'] not in SOURCES:
        raise ValueError('Recovery handoff has an unsupported source')
    if type(result.get('ecid')) is not int or not 0 < result['ecid'] < 2**64:
        raise ValueError('Host USB handoff has an invalid ECID')
    handoff_id, template = result.get('handoff_id'), result.get('template_sha256')
    if (not isinstance(handoff_id, str) or not re.fullmatch(r'[0-9a-f]{32}', handoff_id) or
            int(handoff_id, 16) == 0 or not isinstance(template, str) or
            not re.fullmatch(r'[0-9a-f]{64}', template)):
        raise ValueError('Host USB handoff has invalid attempt provenance')
    return result


def _validate(name, value):
    if name == POLICY_PARAMETER:
        return validate_policy(value)
    if name == HANDOFF_PARAMETER:
        return validate_handoff(value)
    raise ValueError('Unknown recovery identity parameter')


def encode_parameter(name, value):
    value = _validate(name, value)
    raw = json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=True).encode('ascii')
    encoded = base64.urlsafe_b64encode(raw).rstrip(b'=').decode('ascii')
    if len(encoded) > MAX_ENCODED_BYTES:
        raise ValueError('Recovery identity token exceeds its bound')
    return name + '=' + encoded


def _decode(name, value):
    if not 1 <= len(value) <= MAX_ENCODED_BYTES or not re.fullmatch(r'[A-Za-z0-9_-]+', value):
        raise ValueError('Invalid recovery handoff token')
    try:
        raw = base64.b64decode(value + '=' * (-len(value) % 4), altchars=b'-_', validate=True)
    except (ValueError, binascii.Error) as error:
        raise ValueError('Invalid recovery handoff encoding') from error
    if base64.urlsafe_b64encode(raw).rstrip(b'=').decode('ascii') != value:
        raise ValueError('Recovery handoff encoding is not canonical')
    return _validate(name, object_json(raw))


def parameters(cmdline):
    """Return present identity parameters; callers decide which ones are required."""
    if isinstance(cmdline, bytes):
        try:
            cmdline = cmdline.decode('ascii')
        except UnicodeError as error:
            raise ValueError('Recovery command line is not ASCII') from error
    if not isinstance(cmdline, str) or not cmdline.isascii():
        raise ValueError('Recovery command line is not ASCII')
    # /proc/cmdline adds one newline to the kernel's bootargs string.
    if cmdline.endswith('\n'):
        cmdline = cmdline[:-1]
    if len(cmdline) > MAX_CMDLINE_BYTES or any(value in cmdline for value in ('\0', '\r', '\n')):
        raise ValueError('Recovery command line exceeds its bound or contains a control character')
    result = {}
    for token in cmdline.split():
        name, separator, value = token.partition('=')
        if name not in (POLICY_PARAMETER, HANDOFF_PARAMETER):
            if name.startswith('orchard.recovery-'):
                raise ValueError('Unknown recovery identity parameter')
            continue
        if name in result or not separator:
            raise ValueError('Repeated or incomplete recovery identity parameter')
        result[name] = _decode(name, value)
    return result
