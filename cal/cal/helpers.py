from cal.constants import GOOGLE_CALENDAR_COLORS
from django.core.serializers.json import DjangoJSONEncoder
from django.http import HttpResponse
from django.utils import timezone
from datetime import timedelta

import json
import pytz


def json_response(data, status=200):
    return HttpResponse(json.dumps(data, cls=DjangoJSONEncoder), status=status, content_type="application/json")


def ensure_timezone_awareness(dt, optional_timezone=None):
    """
    Ensures that a datetime is timezone aware, and strips off seconds and microseconds.
    `dt`: the datetime
    `optional_timezone`: string representation of a timezone
    """
    if timezone.is_naive(dt):
        if optional_timezone:
            dt = timezone.make_aware(dt, pytz.timezone(optional_timezone))
        else:
            dt = timezone.make_aware(dt, timezone.get_default_timezone())
    dt = dt.astimezone(timezone.utc)
    # Remove seconds and microseconds
    dt = dt.replace(second=0, microsecond=0)
    return dt


EDGE_OPTIONS = set(['inclusive', 'exclusive', 'truncated'])
def truncated_queryset(queryset, edge, start, end):
    """
    Takes in a QuerySet and returns the final Iterable(List) of Events.

    `edge`: Whether events overlapping with the start/end boundaries
            will be included. Options are 'inclusive', 'exclusive', and 'truncated'.
            'truncated' means that events that overlap will be included, but will be
            modified so that they start or end exactly at the boundary they overlap
            with.
    """
    from cal.models import InvalidParameterException

    if edge not in EDGE_OPTIONS:
        raise InvalidParameterException("Edge query parameter {} is not one of {}".format(edge, EDGE_OPTIONS))

    if edge == 'truncated':
        start_edge = list(queryset.filter(start__lt=start, end__gt=start).order_by('start'))
        exclusive = list(queryset.filter(start__gte=start, end__lte=end).order_by('start'))
        end_edge = list(queryset.filter(start__lt=end, end__gt=end).order_by('start'))
        for s in start_edge:
            s.start = start
        for e in end_edge:
            e.end = end
        queryset = start_edge + exclusive + end_edge
    elif edge == 'exclusive':
        queryset = queryset.filter(start__gte=start, end__lte=end).order_by('start')
    elif edge == 'inclusive' or not edge:
        queryset = queryset.filter(end__gt=start, start__lt=end).order_by('start')

    return queryset


def get_color(calendar, color_index):
    """
    Takes in a calendar and a color_index and returns the associated color codes
    from constants.py.
    """
    if color_index == "1":
        return GOOGLE_CALENDAR_COLORS['calendar'].get(calendar.color_index)
    else:
        return GOOGLE_CALENDAR_COLORS['event'].get(color_index)


class EventCollection:
    """
    Represents a Set of events
    """

    def __init__(self, events_func=None, name=None):
        if events_func:
            self._events_func = events_func
        else:
            self._events_func = lambda: set([])

        self._name = name if name else "EventCollection"

    def get_events(self):
        """
        Returns a set of events.
        """
        return self._events_func()

    def intersection(self, other):

        def lazy_get_events():
            return set.intersection(self.get_events(), other.get_events())

        ec = EventCollection(events_func=lazy_get_events, name="({} intersection {})".format(self, other))

        return ec

    def union(self, other):

        def lazy_get_events():
            return set.union(self.get_events(), other.get_events())

        ec = EventCollection(events_func=lazy_get_events, name="({} union {})".format(self, other))

        return ec

    def total_time(self, calendar=None):

        events = self.get_events()

        total = timedelta()
        for e in events:
            total += e.end - e.start

        return int(total.total_seconds())


class TimeNodeChain(EventCollection):

    """
    A data structure that functions as a wrapper around a linked list of TimeNodes.
    It represents how someone is spending their time at different events. And since
    no one can be at two events at the same time, TimeNodes will overwrite any
    conflicting TimeNodes on insert.
    """

    def __init__(self, timenodes=None):
        """
        Initializes a TimeNodeChain, and, if supplied, inserts an iterable of timenodes.
        """
        self.head = None
        self._length = None
        self._total_time = None
        if timenodes:
            self.insert_all(timenodes)
        self.length

    def get_events(self):
        events = set()
        current = self.get_head()
        while current:
            events.add(current)
            current = current.next

        return events

    def get_head(self):
        return self.head

    @property
    def length(self):
        if not self._length:
            current = self.get_head()
            self._length = 0
            while current:
                self._length += 1
                current = current.next
        return self._length

    @property
    def total_time(self):
        """
        Returns the total time in seconds in this TimeNodeChain.
        Overrides EventCollection.total_time with memoized version
        """
        if not self._total_time:
            total = timedelta()
            current = self.get_head()
            while current and current.next:
                total += current.end - current.start
                current = current.next

            self._total_time = total.total_seconds()

        return self._total_time

    def insert(self, timenode, return_overwrites=False):
        """
        Inserts a single TimeNode in O(n) time and returns the inserted node.
        If return_overwrites is set to True, a set of overwritten nodes will be returned as well.
        Wrapper function for TimeNode.insert, so that TimeNodeChain().insert(node) mutates the chain object
        """
        if self.head:
            current = self.head.insert(timenode)
            while current.prev:
                current = current.prev
            self.head = current

        else:
            self.head = timenode

        self._length = None
        self._total_time = None

        return timenode

    def insert_all(self, timenodes, return_overwrites=False):
        """
        Inserts an iterable of TimeNodes (list, QuerySet).
        If return_overwrites is set to True, a set of overwritten nodes will be returned
        If the list is ordered by start time, this operation will take roughly O(n)
        """
        if len(timenodes) == 0:
            return

        # Add the first node to avoid erroring on None.insert
        if self.head:
            last = self.head
            last.insert(timenodes[0])
        else:
            self.head = timenodes[0]
            last = self.head

        for i in range(1, len(timenodes)):
            node = timenodes[i]
            last.insert(node)
            last = node

        while last.prev:
            last = last.prev
        self.head = last

        self.length
        self.total_time

    def get_inverse(self):
        """
        Returns a TimeNodeChain representing the gaps between TimeNodes

        Example: If a TimeNode chain has nodes 1-2, 3-4, 5-6, 6-9,
        get_inverse() returns a chain with nodes 2-3, 4-5
        """
        # TODO use this somewhere to encourage completeness
        if not self.head:
            return None

        current = self.head
        chain = TimeNodeChain()
        last = None
        while current.next:
            if current.end == current.next.start:
                current = current.next
                continue
            elif current.end > current.next.start:
                raise Exception("Inconsistent start and end times between TimeNodes {} and {}".
                        format(current.id, current.next.id))

            node = TimeNode(current.end, current.next.start, "GAP: {}--{}".format(current.id, current.next.id))
            if last:
                last.insert(node)
            else:
                chain.insert(node)
                last = node

            current = current.next

        return chain

    def __str__(self):
        if not self.head:
            return "<Empty TimeNodeChain>"
        else:
            current = self.head
            counter = 0
            result = ""
            while current.next and counter < 10:
                result += "<{}>".format(current.id)
                current = current.next
                counter += 1
            return result


class TimeNode:
    """
    Class representing a block of time.
    """

    def __init__(self, start, end, id=None):
        self.id = id
        self.prev = None
        self.next = None
        self.start = start
        self.end = end

    def insert(self, timenode, return_overwrites=False):
        """
        Inserts a single TimeNode in O(n) time and returns the current node.
        """

        # Basic sanity chex
        if timenode.prev:
            print "Warning! Timenode '{}' to be inserted has a prev".format(timenode.id)
        if timenode.next:
            print "Warning! Timenode '{}' to be inserted has a next".format(timenode.id)
        if not timenode.start or not timenode.end or timenode.start > timenode.end:
            raise Exception("Timenode missing start or end time, or start time > end time")

        def try_insert(node):
            if not node.start or not node.end or node.start > node.end:
                raise Exception("Base node missing start or end time, or start time > end time")
            if timenode.start >= node.end:
                if node.next:
                    # Handle the case of a "sandwiched" node
                    if node.next.start >= timenode.end:
                        node.next.prev = timenode
                        timenode.next = node.next
                        node.next = timenode
                        timenode.prev = node
                    else:
                        return False, node.next
                else:
                    if timenode.prev:
                        print "Warning! Overwriting timenode.prev"
                    node.next = timenode
                    timenode.prev = node
                return True, None
            elif timenode.end <= node.start:
                if node.prev:
                    # Handle the case of a "sandwiched" node
                    if node.prev.end <= timenode.start:
                        node.prev.next = timenode
                        timenode.prev = node.prev
                        node.prev = timenode
                        timenode.next = node
                    else:
                        return False, node.prev
                else:
                    if timenode.next:
                        print "Warning! Overwriting timenode.next of timenode '{}'".format(timenode.id)
                    node.prev = timenode
                    timenode.next = node
                return True, None
            else:
                # Remove the current node
                if node.prev:
                    node.prev.next = node.next
                if node.next:
                    node.next.prev = node.prev
                # Recurse; call insert on an adjacent node or return node
                if node.next:
                    return False, node.next
                elif node.prev:
                    return False, node.prev
                return True, None

        inserted = None
        next_node = self
        while not inserted:
            inserted, next_node = try_insert(next_node)

        return timenode

    def old_insert(self, timenode):
        """
        Inserts a single TimeNode in O(n) time and returns the current node.
        """
        # TODO make this return the inserted node

        # Basic sanity chex
        if not self.start or not self.end or self.start > self.end:
            raise Exception("Base node missing start or end time, or start time > end time")
        if not timenode.start or not timenode.end or timenode.start > timenode.end:
            raise Exception("Timenode missing start or end time, or start time > end time")
        if timenode.prev:
            print "Warning! Timenode '{}' to be inserted has a prev".format(timenode.id)
        if timenode.next:
            print "Warning! Timenode '{}' to be inserted has a next".format(timenode.id)

        if timenode.start >= self.end:
            if self.next:
                # Handle the case of a "sandwiched" node
                if self.next.start >= timenode.end:
                    self.next.prev = timenode
                    timenode.next = self.next
                    self.next = timenode
                    timenode.prev = self
                else:
                    self.next = self.next.insert(timenode)
                    self.next.prev = self
            else:
                if timenode.prev:
                    print "Warning! Overwriting timenode.prev"
                self.next = timenode
                timenode.prev = self
            return self
        elif timenode.end <= self.start:
            if self.prev:
                # Handle the case of a "sandwiched" node
                if self.prev.end <= timenode.start:
                    self.prev.next = timenode
                    timenode.prev = self.prev
                    self.prev = timenode
                    timenode.next = self
                else:
                    self.prev = self.prev.insert(timenode)
                    self.prev.next = self
            else:
                if timenode.next:
                    print "Warning! Overwriting timenode.next of timenode '{}'".format(timenode.id)
                self.prev = timenode
                timenode.next = self
            return self
        else:
            # Remove the current node
            if self.prev:
                self.prev.next = self.next
            if self.next:
                self.next.prev = self.prev

            # Recurse; call insert on an adjacent node or return self
            if self.next:
                self.next.insert(timenode)
            elif self.prev:
                self.prev.insert(timenode)

            return timenode
