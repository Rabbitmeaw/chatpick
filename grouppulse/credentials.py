"""仅管理本应用的凭据；macOS使用系统钥匙串，不将密钥放入命令参数或数据库。"""

import ctypes
import hashlib
import sys


class CredentialVault:
    def __init__(self, path):
        self.service = ('GroupPulse.' + hashlib.sha256(str(path.resolve()).encode()).hexdigest()[:20]).encode()
        self.values = {}
        self.storage = 'session-memory'
        if sys.platform != 'darwin':
            return
        try:
            self.security = ctypes.CDLL('/System/Library/Frameworks/Security.framework/Security')
            self.cf = ctypes.CDLL('/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation')
            ptr, uint, string = ctypes.c_void_p, ctypes.c_uint32, ctypes.c_char_p
            self.security.SecKeychainFindGenericPassword.argtypes = [ptr,uint,string,uint,string,ctypes.POINTER(uint),ctypes.POINTER(ptr),ctypes.POINTER(ptr)]
            self.security.SecKeychainAddGenericPassword.argtypes = [ptr,uint,string,uint,string,uint,string,ctypes.POINTER(ptr)]
            self.security.SecKeychainItemModifyAttributesAndData.argtypes = [ptr,ptr,uint,string]
            self.security.SecKeychainItemFreeContent.argtypes = [ptr,ptr]
            self.security.SecKeychainItemDelete.argtypes = [ptr]
            self.cf.CFRelease.argtypes = [ptr]
            self.storage = 'macos-keychain'
        except (OSError, AttributeError):
            self.storage = 'session-memory'

    def find(self, key, password=True):
        account, length = key.encode(), ctypes.c_uint32()
        data, item = ctypes.c_void_p(), ctypes.c_void_p()
        status = self.security.SecKeychainFindGenericPassword(None, len(self.service), self.service,
            len(account), account, ctypes.byref(length) if password else None,
            ctypes.byref(data) if password else None, ctypes.byref(item))
        if status == -25300:
            return '', None
        if status != 0:
            raise ValueError('无法访问拾聊的系统钥匙串，请解锁钥匙串或允许本应用访问后重试。')
        try:
            value = ctypes.string_at(data, length.value).decode() if password else ''
        finally:
            if data:
                self.security.SecKeychainItemFreeContent(None, data)
        return value, item

    def get(self, key):
        if self.storage != 'macos-keychain':
            return self.values.get(key, '')
        value, item = self.find(key)
        if item:
            self.cf.CFRelease(item)
        return value

    def set(self, key, value):
        if self.storage != 'macos-keychain':
            self.values[key] = value
            return
        _, item = self.find(key, password=False)
        encoded = value.encode()
        if item:
            status = self.security.SecKeychainItemModifyAttributesAndData(item, None, len(encoded), encoded)
        else:
            account = key.encode()
            item = ctypes.c_void_p()
            status = self.security.SecKeychainAddGenericPassword(None, len(self.service), self.service,
                len(account), account, len(encoded), encoded, ctypes.byref(item))
        if item:
            self.cf.CFRelease(item)
        if status != 0:
            raise ValueError('密钥未能保存到系统钥匙串，请解锁或允许保存后重试；未写入普通文件。')

    def delete(self, key):
        if self.storage != 'macos-keychain':
            self.values.pop(key, None)
            return
        _, item = self.find(key, password=False)
        if item:
            status = self.security.SecKeychainItemDelete(item)
            self.cf.CFRelease(item)
            if status != 0:
                raise ValueError('系统钥匙串未能清除该密钥，请允许本应用访问后重试。')
