"""
This module uses Dragodis's Flowchart object in order to calculate code paths.
"""
import functools
from copy import deepcopy

import logging
from typing import Optional, Iterable

from dragodis import NotExistError
from dragodis.interface import Flowchart, BasicBlock

logger = logging.getLogger(__name__)


class PathNode:
    """
    Represents a linked-list of objects constituting a path from a specific node to the function entry point node.
    This object can also track cpu context up to a certain address.
    """

    _cache = {}

    def __new__(cls, block: "BasicBlock", prev: Optional["PathNode"]):
        """
        Constructor that caches and reuses existing instances.
        """
        key = (block, prev)
        try:
            return cls._cache[key]
        except KeyError:
            self = super().__new__(cls)
            self.__init__(block, prev)
            cls._cache[key] = self
            return self

    def __init__(self, block: "BasicBlock", prev: Optional["PathNode"]):
        """
        Initialize a path node.

        :param block: The underlying basic block for this node.
        :param prev: The parent node that points to this node.
        """
        self.block = block
        self.prev = prev
        self._context = None
        self._context_address = None  # address that the context has been filled to (but not including)
        self._init_context = None     # the context used at the starting path

    @classmethod
    def iter_all(cls, block: BasicBlock, _visited=None) -> Iterable["PathNode"]:
        """
        Iterates all tail path nodes from a given block.

        :param block: Block to obtain all path nodes.
        :param _visited: Internally used.

        :yields: PathNode objects that represent the last entry of the path linked list.
        """
        if _visited is None:
            _visited = set()

        # Otherwise generate path nodes and cache results for next time.
        _visited.add(block.start)

        parents = list(block.blocks_to)
        if not parents:
            yield cls(block, prev=None)
        else:
            for parent in parents:
                if parent.start in _visited:
                    continue

                # Create path nodes for each path of parent.
                for parent_path in cls.iter_all(parent, _visited=_visited):
                    yield cls(block, prev=parent_path)

        _visited.remove(block.start)

    def __len__(self):
        return 1 + (len(self.prev) if self.prev else 0)

    @functools.singledispatchmethod
    def __contains__(self, addr):
        return False

    @__contains__.register
    def _(self, addr: int):
        return addr in self.block

    @__contains__.register
    def _(self, block: BasicBlock):
        return any(_block == block for _block in self)

    def __repr__(self):
        return f"Path({' -> '.join(hex(block.start) for block in self)})"

    def __iter__(self) -> Iterable[BasicBlock]:
        """
        Iterates blocks from the root block to the current.
        """
        if self.prev:
            yield from self.prev
        yield self.block

    def __reversed__(self) -> Iterable[BasicBlock]:
        """
        Iterates blocks from the current block up to the root.
        """
        yield self.block
        if self.prev:
            yield from reversed(self.prev)

    def cpu_context(self, addr: int = None, *, init_context: "ProcessorContext"):
        """
        Returns the cpu context filled to (but not including) the specified ea.

        :param int addr: address of interest (defaults to the last ea of the block)
        :param init_context: Initial context to use for the start of the path. (required)

        :return cpu_context.ProcessorContext: cpu context
        """
        if addr is not None and addr not in self.block:
            raise KeyError(
                f"Provided address 0x{addr:X} not in this block "
                f"(0x{self.block.start:X} :: 0x{self.block.end:X})"
            )

        # Determine address to stop computing.
        if addr is None:
            end = self.block.end  # end of a BasicBlock is the first address after the last instruction.
        else:
            end = addr

        # Determine if we need to force the creation of a new context if we have a different init_context.
        new_init_context = self._init_context != init_context
        self._init_context = init_context

        assert end is not None
        # Fill context up to requested endpoint.
        if self._context_address != end or new_init_context:
            # Create context if:
            #   - not created
            #   - current context goes past requested ea
            #   - given init_context is different from the previously given init_context.
            if not self._context or self._context_address > end or new_init_context:
                # Need to check if there is a prev, if not, then we need to create a default context here...
                if self.prev:
                    self._context = self.prev.cpu_context(init_context=init_context)
                    # Modify the context for the current branch if required
                    self._context.prep_for_branch(self.block.start)
                else:
                    self._context = deepcopy(init_context)

                self._context_address = self.block.start

            if self._context_address != end:
                # Fill context up to requested ea.
                logger.debug("Emulating instructions 0x%08X -> 0x%08X", self._context_address, end)
                for line in self.block.lines(start=self._context_address):
                    if line.address == end:
                        break
                    self._context.execute(line.address)

            self._context_address = end

        # Set the next instruction pointer to be the end instruction that we did NOT execute.
        self._context.ip = end

        return deepcopy(self._context)


def iter_paths(flowchart: Flowchart, addr: int) -> Iterable[PathNode]:
    """
    Given an EA, iterate over the paths to the EA.

    For usage example, see Emulator.iter_context_at()

    ..warning:: DO NOT WRAP THIS GENERATOR IN list()!!!  This generator will iterate all possible paths to the node containing
    the specified EA.  On functions containing large numbers of jumps, the number of paths grows exponentially and
    you WILL hit memory exhaustion limits, extremely slow run times, etc. Use extremely conservative constraints
    when iterating.  Nodes containing up to at least 32,768 paths are computed in a reasonably sane amount of time,
    though it probably doesn't make much sense to check this many paths for the data you are looking for.

    :param flowchart: Dragodis Flowchart to get basic blocks from.
    :param addr: Address of interest

    :yield: a path to the object
    """
    # Obtain the block containing the address of interest
    try:
        block = flowchart.get_block(addr)
    except NotExistError:
        # If block not found, then there are no paths to it.
        logger.debug(f"Unable to find block with ea: 0x{addr:08X}")
        return

    yield from PathNode.iter_all(block)
