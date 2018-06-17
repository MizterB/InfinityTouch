import datetime
import os
import pickle
import glob

CACHE_EXT = "cache"

class ExpiringFileCache(object):
    @staticmethod
    def read(filePath):
        return ExpiringCacheFile(filePath).read()

    @staticmethod
    def write(filePath, data=None, secondsToLive=None):
        ExpiringCacheFile(filePath).write(data, secondsToLive)

    @staticmethod
    def exists(filePath, ignoreExt=False):
        if ignoreExt:
            pattern = os.path.splitext(filePath)[0] + "*"
            matches = glob.glob(pattern)
            return len(matches) > 0
        else:
            return ExpiringCacheFile(filePath).exists()

    @staticmethod
    def remove(filePath):
        ExpiringCacheFile(filePath).remove()

class ExpiringCacheFile:
    filePath = None
    data = None
    expires = None

    def __init__(self, filePath):
        self.filePath = filePath
        self.cacheFilePath = "{}.{}".format(filePath, CACHE_EXT)
        self.data = None
        self.expires = None

        # If the file exists, load its contents
        if os.path.exists(self.cacheFilePath):
            with open(self.cacheFilePath, "rb") as pickleFile:
                pickleData = pickle.load(pickleFile)
                self.data = pickleData.get("data", None)
                self.expires = pickleData.get("expires", None)

    def read(self):
        if self.isExpired():
            self.remove()
        return self.data

    def write(self, data=None, secondsToLive=None):
        self.data = data
        if secondsToLive is not None:
            self.expires = self.getExpiration(secondsToLive)
        else:
            self.expires = None

        pickleData = {
            "data": self.data,
            "expires": self.expires
        }

        with open(self.cacheFilePath, "wb") as f:
            f.write(pickle.dumps(pickleData))

    def exists(self):
        if os.path.exists(self.cacheFilePath):
            if not self.isExpired():
                return True
            else:
                os.remove(self.cacheFilePath)
        return False

    def remove(self):
        if os.path.exists(self.cacheFilePath):
            os.remove(self.cacheFilePath)
        self.data = None
        self.expires = None

    def getExpiration(self, seconds=None):
        if seconds is None:
            return None
        expiration = datetime.datetime.now() + datetime.timedelta(seconds=int(seconds))
        return expiration.timestamp()

    def isExpired(self):
        return self.expires is not None and self.expires < datetime.datetime.now().timestamp()