import os
import random
import asyncio
import logging

from rpcudp.protocol import RPCProtocol

from kademlia.node import Node
from kademlia.routing import RoutingTable
from kademlia.utils import digest, touch_dir

log = logging.getLogger(__name__)  # pylint: disable=invalid-name
data_path = os.path.join(os.path.dirname(os.path.realpath(__file__)), "store")
files_path = os.path.join(data_path, "files")


class KademliaProtocol(RPCProtocol):
    def __init__(self, source_node, storage, ksize):
        RPCProtocol.__init__(self)
        self.router = RoutingTable(self, ksize, source_node)
        self.storage = storage
        self.source_node = source_node
        touch_dir(data_path)
        touch_dir(files_path)

    def get_refresh_ids(self):
        """
        Get ids to search for to keep old buckets up to date.
        """
        ids = []
        for bucket in self.router.lonely_buckets():
            rid = random.randint(*bucket.range).to_bytes(20, byteorder='big')
            ids.append(rid)
        return ids

    @staticmethod
    def rpc_stun(sender):  # pylint: disable=no-self-use
        return sender

    def rpc_ping(self, sender, nodeid):
        source = Node(nodeid, sender[0], sender[1])
        self.welcome_if_new(source)
        return self.source_node.id

    def rpc_store(self, sender, nodeid, key, value):
        source = Node(nodeid, sender[0], sender[1])
        self.welcome_if_new(source)
        log.debug("got a store request from %s, storing '%s'='%s'",
                  sender, key.hex(), value)
        self.storage[key] = value
        return True

    def rpc_file_store(self, sender, node_id, key, value):
        """
        Store the data in a file.
        @param sender: source node.
        @param node_id: source node.
        @param key: Key that will be the file name.
        @param value: The data that will be inside the file.
        """
        source = Node(node_id, sender[0], sender[1])
        self.welcome_if_new(source)
        log.debug(f"got a store request from {sender}, storing '{key.hex}'='{value}'")
        try:
            with open(os.path.join(files_path, key.hex() + ".kad"), "wb") as f:
                f.write(value)
        except TypeError:
            os.remove(os.path.join(files_path, key.hex() + ".kad"))
            with open(os.path.join(data_path, key.hex() + ".txt"), "w") as f:
                f.write(value)

        return True

    def rpc_find_file(self, sender, node_id, key):
        """
        Find in the actual node the file based on the key.
        @param sender: source node.
        @param node_id: source node.
        @param key: The key we want to find.
        @return: value if found, else `None`.
        """
        source = Node(node_id, sender[0], sender[1])
        self.welcome_if_new(source)
        try:
            with open(os.path.join(files_path, key.hex() + ".kad"), "rb") as f:
                return {'value': f.read()}
        except FileNotFoundError:
            try:
                with open(os.path.join(data_path, key.hex() + ".txt"), "r") as f:
                    return {'value': f.read()}
            except FileNotFoundError:
                return {'value': None}

    def rpc_find_node(self, sender, nodeid, key):
        log.info("finding neighbors of %i in local table",
                 int(nodeid.hex(), 16))
        source = Node(nodeid, sender[0], sender[1])
        self.welcome_if_new(source)
        node = Node(key)
        neighbors = self.router.find_neighbors(node, exclude=source)
        return list(map(tuple, neighbors))

    def rpc_find_value(self, sender, nodeid, key):
        source = Node(nodeid, sender[0], sender[1])
        self.welcome_if_new(source)
        value = self.storage.get(key, None)
        if value is None:
            return self.rpc_find_node(sender, nodeid, key)
        return {'value': value}

    async def call_find_node(self, node_to_ask, node_to_find):
        address = (node_to_ask.ip, node_to_ask.port)
        result = await self.find_node(address, self.source_node.id,
                                      node_to_find.id)
        return self.handle_call_response(result, node_to_ask)

    async def call_find_value(self, node_to_ask, node_to_find):
        address = (node_to_ask.ip, node_to_ask.port)
        result = await self.find_value(address, self.source_node.id,
                                       node_to_find.id)
        return self.handle_call_response(result, node_to_ask)

    async def call_file_value(self, node_to_ask, node_to_find):
        """
        Find the file in the node.
        """
        address = (node_to_ask.ip, node_to_ask.port)
        result = await self.find_file(address, self.source_node.id, node_to_find.id)
        return self.handle_call_response(result, node_to_ask)

    async def call_ping(self, node_to_ask):
        address = (node_to_ask.ip, node_to_ask.port)
        result = await self.ping(address, self.source_node.id)
        return self.handle_call_response(result, node_to_ask)

    async def call_file_store(self, node_to_ask, key, value):
        """
        Replace variable storage with file storage for file saving and persistence.
        """
        address = (node_to_ask.ip, node_to_ask.port)
        result = await self.file_store(address, self.source_node.id, key, value)
        return self.handle_call_response(result, node_to_ask)

    async def call_store(self, node_to_ask, key, value):
        address = (node_to_ask.ip, node_to_ask.port)
        result = await self.store(address, self.source_node.id, key, value)
        return self.handle_call_response(result, node_to_ask)

    def welcome_if_new(self, node):
        """
        Given a new node, send it all the keys/values it should be storing,
        then add it to the routing table.

        @param node: A new node that just joined (or that we just found out
        about).

        Process:
        For each key in storage, get k closest nodes.  If newnode is closer
        than the furtherst in that list, and the node for this server
        is closer than the closest in that list, then store the key/value
        on the new node (per section 2.5 of the paper)
        """
        if not self.router.is_new_node(node):
            return

        log.info("never seen %s before, adding to router", node)
        for key, value in self.storage:
            keynode = Node(digest(key))
            neighbors = self.router.find_neighbors(keynode)
            if neighbors:
                last = neighbors[-1].distance_to(keynode)
                new_node_close = node.distance_to(keynode) < last
                first = neighbors[0].distance_to(keynode)
                this_closest = self.source_node.distance_to(keynode) < first
            if not neighbors or (new_node_close and this_closest):
                asyncio.ensure_future(self.call_store(node, key, value))
        self.router.add_contact(node)

    def handle_call_response(self, result, node):
        """
        If we get a response, add the node to the routing table.  If
        we get no response, make sure it's removed from the routing table.
        """
        if not result[0]:
            log.warning("no response from %s, removing from router", node)
            self.router.remove_contact(node)
            return result

        log.info("got successful response from %s", node)
        self.welcome_if_new(node)
        return result
