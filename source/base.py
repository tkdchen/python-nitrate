# ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
#
#   Python API for the Nitrate test case management system.
#   Copyright (c) 2012 Red Hat, Inc. All rights reserved.
#   Author: Petr Splichal <psplicha@redhat.com>
#
# ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
#
#   This library is free software; you can redistribute it and/or
#   modify it under the terms of the GNU Lesser General Public
#   License as published by the Free Software Foundation; either
#   version 2.1 of the License, or (at your option) any later version.
#
#   This library is distributed in the hope that it will be useful,
#   but WITHOUT ANY WARRANTY; without even the implied warranty of
#   MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the GNU
#   Lesser General Public License for more details.
#
# ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

"""
Nitrate class and internal utilities

Search support
~~~~~~~~~~~~~~

Multiple Nitrate classes provide the static method 'search' which takes
the search query in the Django QuerySet format which gives an easy
access to the foreign keys and basic search operators. For example:

    Product.search(name="Red Hat Enterprise Linux 6")
    TestPlan.search(name__contains="python")
    TestRun.search(manager__email='login@example.com'):
    TestCase.search(script__startswith='/CoreOS/python')

For the complete list of available operators see Django documentation:
https://docs.djangoproject.com/en/dev/ref/models/querysets/#field-lookups
"""

import datetime
import nitrate.config as config
import nitrate.utils as utils
import nitrate.xmlrpc as xmlrpc
import nitrate.teiid as teiid

from nitrate.config import log, Config
from nitrate.xmlrpc import NitrateError

# ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
#  Internal Utilities
# ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

def _getter(field):
    """
    Simple getter factory function.

    For given field generate getter function which calls self._fetch(), to
    initialize instance data if necessary, and returns self._field.
    """

    def getter(self):
        # Initialize the attribute unless already done
        if getattr(self, "_" + field) is NitrateNone:
            self._fetch()
        # Return self._field
        return getattr(self, "_" + field)

    return getter

def _setter(field):
    """
    Simple setter factory function.

    For given field return setter function which calls self._fetch(), to
    initialize instance data if necessary, updates the self._field and
    remembers modifed state if the value is changed.
    """

    def setter(self, value):
        # Initialize the attribute unless already done
        if getattr(self, "_" + field) is NitrateNone:
            self._fetch()
        # Update only if changed
        if getattr(self, "_" + field) != value:
            setattr(self, "_" + field, value)
            log.info(u"Updating {0}'s {1} to '{2}'".format(
                    self.identifier, field, value))
            # Remember modified state if caching
            if config.get_cache_level() != config.CACHE_NONE:
                self._modified = True
            # Save the changes immediately otherwise
            else:
                self._update()

    return setter

def _idify(id):
    """
    Pack/unpack multiple ids into/from a single id

    List of ids is converted into a single id. Single id is converted
    into list of original ids. For example:

        _idify([1, 2]) ---> 1000000002
        _idify(1000000002) ---> [1, 2]

    This is used for indexing by fake internal id.
    """
    if isinstance(id, list):
        result = 0
        for value in id:
            result = result * config._MAX_ID + value
        return result
    elif isinstance(id, int):
        result = []
        while id > 0:
            remainder = id % config._MAX_ID
            id = id / config._MAX_ID
            result.append(int(remainder))
        result.reverse()
        return result
    else:
        raise NitrateError("Invalid id for idifying: '{0}'".format(id))

# ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
#  NitrateNone Class
# ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

class NitrateNone(object):
    """ Used for distinguishing uninitialized values from regular 'None' """
    pass


# ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
#  Nitrate Class
# ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

class Nitrate(object):
    """
    General Nitrate Object.

    Takes care of initiating the connection to the Nitrate server and
    parses user configuration.
    """

    # Unique object identifier. If not None ---> object is initialized
    # (all unknown attributes are set to special value NitrateNone)
    _id = None

    # Timestamp when the object data were fetched from the server.
    # If not None, all object attributes are filled with real data.
    _fetched = None

    # Default expiration for immutable objects is 1 month
    _expiration = datetime.timedelta(days=30)

    # List of all object attributes (used for init & expiration)
    _attributes = []

    _connection = None
    _teiid_instance = None
    _requests = 0
    _multicall_proxy = None
    _identifier_width = 0

    # ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
    #  Nitrate Properties
    # ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

    id = property(_getter("id"), doc="Object identifier.")

    @property
    def identifier(self):
        """ Consistent identifier string """
        # Use id if known
        if self._id not in [None, NitrateNone]:
            id = self._id
        # When unknown use 'ID#UNKNOWN' or 'ID#UNKNOWN (name)'
        else:
            name = getattr(self, "_name", None)
            if name not in [None, NitrateNone]:
                id = "UNKNOWN ({0})".format(name)
            else:
                id = "UNKNOWN"
        return "{0}#{1}".format(
                self._prefix, str(id).rjust(self._identifier_width, "0"))

    @property
    def _server(self):
        """ Connection to the server """

        # Connect to the server unless already connected
        if Nitrate._connection is None:
            log.debug(u"Contacting server {0}".format(
                    Config().nitrate.url))
            # Plain authentication if username & password given
            try:
                Nitrate._connection = xmlrpc.NitrateXmlrpc(
                        Config().nitrate.username,
                        Config().nitrate.password,
                        Config().nitrate.url).server
            # Kerberos otherwise
            except AttributeError:
                Nitrate._connection = xmlrpc.NitrateKerbXmlrpc(
                        Config().nitrate.url).server

        # Return existing connection
        Nitrate._requests += 1
        return Nitrate._connection

    @property
    def _teiid(self):
        """ Connection to the Teiid instance """
        # Create the instance unless already exist
        if Nitrate._teiid_instance is None:
            Nitrate._teiid_instance = teiid.Teiid()
        # Return the instance
        return Nitrate._teiid_instance

    @classmethod
    def _cache_lookup(cls, id, **kwargs):
        """ Look up cached objects, return found instance and search key """
        # ID check
        if isinstance(id, int) or isinstance(id, basestring):
            return cls._cache[id], id

        # Check injet (initial object dictionary) for id
        if isinstance(id, dict):
            return cls._cache[id['id']], id["id"]

        raise KeyError

    @classmethod
    def _is_cached(cls, id):
        """
        Check whether objects are cached (initialized & fetched)

        Accepts object id, list of ids, object or a list of objects.
        Makes sure that the object is in the memory and has attached
        all attributes. For ids, cache index is checked for presence.
        """
        # Check fetch timestamp if object given
        if isinstance(id, Nitrate):
            return id._fetched is not None
        # Check for presence in cache, make sure the object is fetched
        if isinstance(id, int) or isinstance(id, basestring):
            return id in cls._cache and cls._cache[id]._fetched is not None
        # Run recursively for each given id/object if list given
        if isinstance(id, list) or isinstance(id, set):
            return all(cls._is_cached(i) for i in id)
        # Something went wrong
        return False

    @property
    def _is_expired(self):
        """ Check if cached object has expired """
        return self._fetched is None or (
                datetime.datetime.now() - self._fetched) > self._expiration

    def _is_initialized(self, id_or_inject, **kwargs):
        """
        Check whether the object is initialized, handle names & injects

        Takes object id or inject (initial object dict), detects which
        of them was given, checks whether the object has already been
        initialized and returns tuple: (id, name, inject, initialized).
        """
        id = name = inject = None
        # Initial object dict
        if isinstance(id_or_inject, dict):
            inject = id_or_inject
        # Object identified by name
        elif isinstance(id_or_inject, basestring):
            name =  id_or_inject
        # Regular object id
        else:
            id = id_or_inject
        # Initialized objects have the self._id attribute set
        if self._id is None:
            return id, name, inject, False
        # If inject given, fetch data from it (unless already fetched)
        if inject is not None and not self._fetched:
            self._fetch(inject, **kwargs)
        return id, name, inject, True

    @property
    def _multicall(self):
        """
        Enqueue xmlrpc calls if MultiCall enabled otherwise send directly

        If MultiCall mode enabled, put xmlrpc calls to the queue, otherwise
        send them directly to server.
        """
        if Nitrate._multicall_proxy is not None:
            return self._multicall_proxy
        else:
            return self._server

    # ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
    #  Nitrate Special
    # ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

    def __new__(cls, id=None, *args, **kwargs):
        """ Create a new object, handle caching if enabled """
        # No caching when turned of or class does not support it
        if (config.get_cache_level() < config.CACHE_OBJECTS or
                getattr(cls, "_cache", None) is None):
            return super(Nitrate, cls).__new__(cls)
        # Make sure that cache has been initialized
        Cache()
        # Look up cached object by id (or other arguments in kwargs)
        try:
            # If found, we get instance and key by which it was found
            instance, key = cls._cache_lookup(id, **kwargs)
            if isinstance(key, int):
                log.cache("Using cached {0} ID#{1}".format(cls.__name__, key))
            else:
                log.cache("Using cached {0} '{1}'".format(cls.__name__, key))
            return instance
        # Object not cached yet, create a new one and cache it
        except KeyError:
            new = super(Nitrate, cls).__new__(cls)
            if isinstance(id, int):
                log.cache("Caching {0} ID#{1}".format(cls.__name__, id))
                cls._cache[id] = new
            elif isinstance(id, basestring) or "name" in kwargs:
                log.cache("Caching {0} '{1}'".format(
                        cls.__name__, (id or kwargs.get("name"))))
            return new

    def __init__(self, id=None, prefix="ID"):
        """ Initialize the object id, prefix and internal attributes """
        # Set up the prefix
        self._prefix = prefix
        # Initialize internal attributes and reset the fetch timestamp
        self._init()

        # Check and set the object id
        if id is None:
            self._id = NitrateNone
        elif isinstance(id, int):
            self._id = id
        else:
            try:
                self._id = int(id)
            except ValueError:
                raise NitrateError("Invalid {0} id: '{1}'".format(
                        self.__class__.__name__, id))

    def __str__(self):
        """ Provide ascii string representation """
        return utils.ascii(unicode(self))

    def __unicode__(self):
        """ Short summary about the connection """
        return u"Nitrate server: {0}\nTotal requests handled: {1}".format(
                Config().nitrate.url, self._requests)

    def __eq__(self, other):
        """ Objects are compared based on their id """
        # Special handling for comparison with None
        if other is None:
            return False
        # We can only compare objects of the same type
        if self.__class__ != other.__class__:
            raise NitrateError("Cannot compare '{0}' with '{1}'".format(
                self.__class__.__name__, other.__class__.__name__))
        return self.id == other.id

    def __ne__(self, other):
        """ Objects are compared based on their id """
        return not(self == other)

    def __hash__(self):
        """ Use object id as the default hash """
        return self.id

    def __repr__(self):
        """ Object(id) or Object('name') representation """
        # Use the object id by default, name (if available) otherwise
        if self._id is not NitrateNone:
            id = self._id
        elif getattr(self, "_name", NitrateNone) is not NitrateNone:
            id = "'{0}'".format(self._name)
        else:
            id = "<unknown>"
        return "{0}({1})".format(self.__class__.__name__, id)

    # ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
    #  Nitrate Methods
    # ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

    def _init(self):
        """ Set all object attributes to NitrateNone, reset fetch timestamp """
        # Each class is expected to have a list of attributes defined
        for attribute in self._attributes:
            setattr(self, "_" + attribute, NitrateNone)
        # And reset the fetch timestamp
        self._fetched = None

    def _fetch(self, inject=None):
        """ Fetch object data from the server """
        # This is to be implemented by respective class.
        # Here we just save the timestamp when data were fetched.
        self._fetched = datetime.datetime.now()
        # Store the initial object dict for possible future use
        self._inject = inject

    def _index(self, *keys):
        """ Index self into the class cache if caching enabled """
        # Skip indexing completely when caching off
        if config.get_cache_level() < config.CACHE_OBJECTS:
            return
        # Index by ID
        if self._id is not NitrateNone:
            self.__class__._cache[self._id] = self
        # Index each given key
        for key in keys:
            self.__class__._cache[key] = self

# We need to import cache only here because of cyclic import
from nitrate.cache import Cache
