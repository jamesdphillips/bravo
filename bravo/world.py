import random
import sys
import weakref

from twisted.internet.task import LoopingCall
from twisted.python.filepath import FilePath

from nbt.nbt import NBTFile

from bravo.chunk import Chunk
from bravo.serialize import LevelSerializer

def base36(i):
    """
    Return the string representation of i in base 36, using lowercase letters.
    """

    letters = "0123456789abcdefghijklmnopqrstuvwxyz"

    if i < 0:
        i = -i
        signed = True
    elif i == 0:
        return "0"
    else:
        signed = False

    s = ""

    while i:
        i, digit = divmod(i, 36)
        s = letters[digit] + s

    if signed:
        s = "-" + s

    return s

class World(LevelSerializer):
    """
    Object representing a world on disk.

    Worlds are composed of levels and chunks, each of which corresponds to
    exactly one file on disk. Worlds also contain saved player data.
    """

    season = None
    """
    The current `ISeason`.
    """

    saving = True
    """
    Whether objects belonging to this world may be written out to disk.
    """

    def __init__(self, folder):
        """
        Load a world from disk.

        :Parameters:
            folder : str
                The directory containing the world.
        """

        self.folder = FilePath(folder)
        if not self.folder.exists():
            self.folder.makedirs()

        self.chunk_cache = weakref.WeakValueDictionary()
        self.dirty_chunk_cache = dict()

        self.spawn = (0, 0, 0)
        self.seed = random.randint(0, sys.maxint)

        level = self.folder.child("level.dat")
        if level.exists() and level.getsize():
            self.load_from_tag(NBTFile(fileobj=level.open("r")))

        tag = self.save_to_tag()
        tag.write_file(fileobj=level.open("w"))

        self.chunk_management_loop = LoopingCall(self.sort_chunks)
        self.chunk_management_loop.start(1)

    def sort_chunks(self):
        """
        Sort out the internal caches.

        This method will always block when there are dirty chunks.
        """

        first = True

        all_chunks = dict(self.dirty_chunk_cache)
        all_chunks.update(self.chunk_cache)
        self.chunk_cache.clear()
        self.dirty_chunk_cache.clear()
        for coords, chunk in all_chunks.iteritems():
            if chunk.dirty:
                if first:
                    first = False
                    self.save_chunk(chunk)
                    self.chunk_cache[coords] = chunk
                else:
                    self.dirty_chunk_cache[coords] = chunk
            else:
                self.chunk_cache[coords] = chunk

    def save_off(self):
        """
        Disable saving to disk.

        This is useful for accessing the world on disk without Bravo
        interfering, for backing up the world.
        """

        if not self.saving:
            return

        d = dict(self.chunk_cache)
        self.chunk_cache = d
        self.saving = False

    def save_on(self):
        """
        Enable saving to disk.
        """

        if self.saving:
            return

        d = weakref.WeakValueDictionary(self.chunk_cache)
        self.chunk_cache = d
        self.saving = True

    def populate_chunk(self, chunk):
        """
        Recreate data for a chunk.

        This method does arbitrary terrain generation depending on the current
        plugins, and then regenerates the chunk metadata so that the chunk can
        be sent to clients.

        A lot of maths may be done by this method, so do not call it unless
        absolutely necessary, e.g. when the chunk is created for the first
        time.
        """

        for stage in self.pipeline:
            stage.populate(chunk, self.seed)

        chunk.regenerate()

    def load_chunk(self, x, z):
        """
        Retrieve a `Chunk`.

        This method does lots of automatic caching of chunks to ensure that
        disk I/O is kept to a minimum.
        """

        if (x, z) in self.chunk_cache:
            return self.chunk_cache[x, z]
        elif (x, z) in self.dirty_chunk_cache:
            return self.dirty_chunk_cache[x, z]

        chunk = Chunk(x, z)

        f = self.folder.child(base36(x & 63)).child(base36(z & 63))
        if not f.exists():
            f.makedirs()
        f = f.child("c.%s.%s.dat" % (base36(x), base36(z)))
        if f.exists() and f.getsize():
            tag = NBTFile(fileobj=f.open("r"))
            chunk.load_from_tag(tag)

        if chunk.populated:
            self.chunk_cache[x, z] = chunk
        else:
            self.populate_chunk(chunk)
            chunk.populated = True
            chunk.dirty = True

            self.dirty_chunk_cache[x, z] = chunk

        # Apply the current season to the chunk.
        if self.season:
            self.season.transform(chunk)

        # Since this chunk hasn't been given to any player yet, there's no
        # conceivable way that any meaningful damage has been accumulated;
        # anybody loading any part of this chunk will want the entire thing.
        # Thus, it should start out undamaged.
        chunk.clear_damage()

        return chunk

    def save_chunk(self, chunk):

        if not chunk.dirty:
            return

        x, z = chunk.x, chunk.z
        f = self.folder.child(base36(x & 63)).child(base36(z & 63))
        if not f.exists():
            f.makedirs()
        f = f.child("c.%s.%s.dat" % (base36(x), base36(z)))
        tag = chunk.save_to_tag()
        tag.write_file(fileobj=f.open("w"))

        chunk.dirty = False

    def load_player(self, username):
        """
        Retrieve player data.
        """

        player = self.factory.create_entity(self.spawn[0], self.spawn[1],
            self.spawn[2], "Player")

        player.location.stance = self.spawn[1]
        player.username = username

        f = self.folder.child("players")
        if not f.exists():
            f.makedirs()
        f = f.child("%s.dat" % username)
        if f.exists() and f.getsize():
            tag = NBTFile(fileobj=f.open("r"))
            player.load_from_tag(tag)

        return player

    def save_player(self, username, player):

        f = self.folder.child("players")
        if not f.exists():
            f.makedirs()
        f = f.child("%s.dat" % username)
        tag = player.save_to_tag()
        tag.write_file(fileobj=f.open("w"))