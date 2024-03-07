"""
Bencoding implementation written in python3. See https://www.bittorrent.org/beps/bep_0003.html.
For encode/decode a certain object, the decode function takes approximately the same time as the encode function.
Requires sys.version_info >= (3, 6)
"""

from io import BufferedReader, BytesIO
from typing import Union


class BdecodeError(Exception):
    pass


class BencodeError(Exception):
    pass


def bencode(obj):
    fp = []
    write = fp.append

    def _bencode(_obj):
        t = type(_obj)
        if t is int:
            write(b'i')
            write(str(_obj).encode())
            write(b'e')
        elif t is bytes:
            write(str(len(_obj)).encode())
            write(b':')
            write(_obj)
        elif t is str:
            _obj = _obj.encode()
            write(str(len(_obj)).encode())
            write(b':')
            write(_obj)
        elif t is list or t is tuple:
            write(b'l')
            for item in _obj:
                _bencode(item)
            write(b'e')
        elif t is dict:
            write(b'd')
            for key, val in sorted(_obj.items()):
                _bencode(key)
                _bencode(val)
            write(b'e')

    _bencode(obj)
    return b''.join(fp)


def bdecode(_input: Union[bytes, BufferedReader, str]):
    """
    Args:
        _input: A bytes object, or IO BufferedReader, or a file path
    Raises:
        AssertionError
        BdecodeError
    """
    _bytes = _input
    if isinstance(_input, BufferedReader):
        _bytes = _input.read()
        assert isinstance(_bytes, bytes), 'Unsupported input stream'
    elif isinstance(_input, str) and len(_input) < 1024:
        with open(_input, 'rb') as _file:
            _bytes = _file.read()
    assert isinstance(_bytes, bytes), "Unsupported input arg"
    fp = BytesIO(_bytes)
    read = fp.read

    def _bdecode():
        c = read(1)
        if c == b'e':
            return StopIteration
        elif c == b'i':
            values = []
            ch = read(1)
            while ch != b'e':
                values.append(ch)
                ch = read(1)
            return int(b''.join(values))
        elif c == b'l':
            result = []
            while True:
                val = _bdecode()
                if val is StopIteration:
                    return result
                result.append(val)
        elif c == b'd':
            result = {}
            while True:
                key = _bdecode()
                if key is StopIteration:
                    return result
                val = _bdecode()
                result[key] = val
        else:
            size = 0
            while b'0' <= c <= b'9':
                size = size * 10 + (ord(c) - ord('0'))
                c = read(1)
            return read(size)

    return _bdecode()
