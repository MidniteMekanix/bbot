import os
import dns
import time
import asyncio
import logging
import ipaddress
import traceback
from contextlib import suppress
from cachetools import LRUCache

from ..regexes import dns_name_regex
from bbot.core.engine import EngineServer
from bbot.core.errors import DNSWildcardBreak
from bbot.core.helpers.async_helpers import NamedLock
from bbot.core.helpers.misc import (
    clean_dns_record,
    parent_domain,
    domain_parents,
    is_ip,
    is_domain,
    is_ptr,
    is_dns_name,
    host_in_host,
    make_ip_type,
    smart_decode,
    cloudcheck,
    rand_string,
)


log = logging.getLogger("bbot.core.helpers.dns.engine.server")


class DNSEngine(EngineServer):

    CMDS = {
        0: "resolve",
        1: "resolve_event",
        2: "resolve_batch",
        3: "resolve_raw_batch",
        4: "is_wildcard",
        5: "is_wildcard_domain",
        99: "_mock_dns",
    }

    all_rdtypes = ["A", "AAAA", "SRV", "MX", "NS", "SOA", "CNAME", "TXT"]

    def __init__(self, socket_path, config={}):
        super().__init__(socket_path)

        self.config = config
        # config values
        self.timeout = self.config.get("dns_timeout", 5)
        self.retries = self.config.get("dns_retries", 1)
        self.abort_threshold = self.config.get("dns_abort_threshold", 50)
        self.max_dns_resolve_distance = self.config.get("max_dns_resolve_distance", 5)

        # resolver
        self.resolver = dns.asyncresolver.Resolver()
        self.resolver.rotate = True
        self.resolver.timeout = self.timeout
        self.resolver.lifetime = self.timeout

        # skip certain queries
        dns_omit_queries = self.config.get("dns_omit_queries", None)
        if not dns_omit_queries:
            dns_omit_queries = []
        self.dns_omit_queries = dict()
        for d in dns_omit_queries:
            d = d.split(":")
            if len(d) == 2:
                rdtype, query = d
                rdtype = rdtype.upper()
                query = query.lower()
                try:
                    self.dns_omit_queries[rdtype].add(query)
                except KeyError:
                    self.dns_omit_queries[rdtype] = {query}

        # wildcard handling
        self.wildcard_ignore = self.config.get("dns_wildcard_ignore", None)
        if not self.wildcard_ignore:
            self.wildcard_ignore = []
        self.wildcard_ignore = tuple([str(d).strip().lower() for d in self.wildcard_ignore])
        self.wildcard_tests = self.config.get("dns_wildcard_tests", 5)
        self._wildcard_cache = dict()
        # since wildcard detection takes some time, This is to prevent multiple
        # modules from kicking off wildcard detection for the same domain at the same time
        self._wildcard_lock = NamedLock()

        self._dns_connectivity_lock = asyncio.Lock()
        self._last_dns_success = None
        self._last_connectivity_warning = time.time()
        # keeps track of warnings issued for wildcard detection to prevent duplicate warnings
        self._dns_warnings = set()
        self._errors = dict()
        self._debug = self.config.get("dns_debug", False)
        self._dns_cache = LRUCache(maxsize=10000)

        self.filter_bad_ptrs = self.config.get("dns_filter_ptrs", True)

    async def resolve(self, query, **kwargs):
        """Resolve DNS names and IP addresses to their corresponding results.

        This is a high-level function that can translate a given domain name to its associated IP addresses
        or an IP address to its corresponding domain names. It's structured for ease of use within modules
        and will abstract away most of the complexity of DNS resolution, returning a simple set of results.

        Args:
            query (str): The domain name or IP address to resolve.
            **kwargs: Additional arguments to be passed to the resolution process.

        Returns:
            set: A set containing resolved domain names or IP addresses.

        Examples:
            >>> results = await resolve("1.2.3.4")
            {"evilcorp.com"}

            >>> results = await resolve("evilcorp.com")
            {"1.2.3.4", "dead::beef"}
        """
        results = set()
        errors = []
        try:
            answers, errors = await self.resolve_raw(query, **kwargs)
            for answer in answers:
                for _, host in self.extract_targets(answer):
                    results.add(host)
        except BaseException:
            log.trace(f"Caught exception in resolve({query}, {kwargs}):")
            log.trace(traceback.format_exc())
            raise

        self.debug(f"Results for {query} with kwargs={kwargs}: {results}")
        return results

    async def resolve_raw(self, query, **kwargs):
        """Resolves the given query to its associated DNS records.

        This function is a foundational method for DNS resolution in this class. It understands both IP addresses and
        hostnames and returns their associated records in a raw format provided by the dnspython library.

        Args:
            query (str): The IP address or hostname to resolve.
            type (str or list[str], optional): Specifies the DNS record type(s) to fetch. Can be a single type like 'A'
                or a list like ['A', 'AAAA']. If set to 'any', 'all', or '*', it fetches all supported types. If not
                specified, the function defaults to fetching 'A' and 'AAAA' records.
            **kwargs: Additional arguments that might be passed to the resolver.

        Returns:
            tuple: A tuple containing two lists:
                - list: A list of tuples where each tuple consists of a record type string (like 'A') and the associated
                  raw dnspython answer.
                - list: A list of tuples where each tuple consists of a record type string and the associated error if
                  there was an issue fetching the record.

        Examples:
            >>> await resolve_raw("8.8.8.8")
            ([('PTR', <dns.resolver.Answer object at 0x7f4a47cdb1d0>)], [])

            >>> await resolve_raw("dns.google")
            (<dns.resolver.Answer object at 0x7f4a47ce46d0>, [])
        """
        # DNS over TCP is more reliable
        # But setting this breaks DNS resolution on Ubuntu because systemd-resolve doesn't support TCP
        # kwargs["tcp"] = True
        try:
            query = str(query).strip()
            kwargs.pop("rdtype", None)
            rdtype = kwargs.pop("type", "A")
            if is_ip(query):
                return await self._resolve_ip(query, **kwargs)
            else:
                return await self._resolve_hostname(query, rdtype=rdtype, **kwargs)
        except BaseException:
            log.trace(f"Caught exception in resolve_raw({query}, {kwargs}):")
            log.trace(traceback.format_exc())
            raise

    async def _resolve_hostname(self, query, **kwargs):
        """Translate a hostname into its corresponding IP addresses.

        This is the foundational function for converting a domain name into its associated IP addresses. It's designed
        for internal use within the class and handles retries, caching, and a variety of error/timeout scenarios.
        It also respects certain configurations that might ask to skip certain types of queries. Results are returned
        in the default dnspython answer object format.

        Args:
            query (str): The hostname to resolve.
            rdtype (str, optional): The type of DNS record to query (e.g., 'A', 'AAAA'). Defaults to 'A'.
            retries (int, optional): The number of times to retry on failure. Defaults to class-wide `retries`.
            use_cache (bool, optional): Whether to check the cache before trying a fresh resolution. Defaults to True.
            **kwargs: Additional arguments that might be passed to the resolver.

        Returns:
            tuple: A tuple containing:
                - list: A list of resolved IP addresses.
                - list: A list of errors encountered during the resolution process.

        Examples:
            >>> results, errors = await _resolve_hostname("google.com")
            (<dns.resolver.Answer object at 0x7f4a4b2caf50>, [])
        """
        self.debug(f"Resolving {query} with kwargs={kwargs}")
        results = []
        errors = []
        rdtype = kwargs.get("rdtype", "A")

        # skip certain queries if requested
        if rdtype in self.dns_omit_queries:
            if any(h == query or query.endswith(f".{h}") for h in self.dns_omit_queries[rdtype]):
                self.debug(f"Skipping {rdtype}:{query} because it's omitted in the config")
                return results, errors

        parent = parent_domain(query)
        retries = kwargs.pop("retries", self.retries)
        use_cache = kwargs.pop("use_cache", True)
        tries_left = int(retries) + 1
        parent_hash = hash(f"{parent}:{rdtype}")
        dns_cache_hash = hash(f"{query}:{rdtype}")
        while tries_left > 0:
            try:
                if use_cache:
                    results = self._dns_cache.get(dns_cache_hash, [])
                if not results:
                    error_count = self._errors.get(parent_hash, 0)
                    if error_count >= self.abort_threshold:
                        connectivity = await self._connectivity_check()
                        if connectivity:
                            log.verbose(
                                f'Aborting query "{query}" because failed {rdtype} queries for "{parent}" ({error_count:,}) exceeded abort threshold ({self.abort_threshold:,})'
                            )
                            if parent_hash not in self._dns_warnings:
                                log.verbose(
                                    f'Aborting future {rdtype} queries to "{parent}" because error count ({error_count:,}) exceeded abort threshold ({self.abort_threshold:,})'
                                )
                            self._dns_warnings.add(parent_hash)
                            return results, errors
                    results = await self._catch(self.resolver.resolve, query, **kwargs)
                    if use_cache:
                        self._dns_cache[dns_cache_hash] = results
                    if parent_hash in self._errors:
                        self._errors[parent_hash] = 0
                break
            except (
                dns.resolver.NoNameservers,
                dns.exception.Timeout,
                dns.resolver.LifetimeTimeout,
                TimeoutError,
            ) as e:
                try:
                    self._errors[parent_hash] += 1
                except KeyError:
                    self._errors[parent_hash] = 1
                errors.append(e)
                # don't retry if we get a SERVFAIL
                if isinstance(e, dns.resolver.NoNameservers):
                    break
                tries_left -= 1
                err_msg = (
                    f'DNS error or timeout for {rdtype} query "{query}" ({self._errors[parent_hash]:,} so far): {e}'
                )
                if tries_left > 0:
                    retry_num = (retries + 1) - tries_left
                    self.debug(err_msg)
                    self.debug(f"Retry (#{retry_num}) resolving {query} with kwargs={kwargs}")
                else:
                    log.verbose(err_msg)

        if results:
            self._last_dns_success = time.time()
            self.debug(f"Answers for {query} with kwargs={kwargs}: {list(results)}")

        if errors:
            self.debug(f"Errors for {query} with kwargs={kwargs}: {errors}")

        return results, errors

    async def _resolve_ip(self, query, **kwargs):
        """Translate an IP address into a corresponding DNS name.

        This is the most basic function that will convert an IP address into its associated domain name. It handles
        retries, caching, and multiple types of timeout/error scenarios internally. The function is intended for
        internal use and should not be directly called by modules without understanding its intricacies.

        Args:
            query (str): The IP address to be reverse-resolved.
            retries (int, optional): The number of times to retry on failure. Defaults to 0.
            use_cache (bool, optional): Whether to check the cache for the result before attempting resolution. Defaults to True.
            **kwargs: Additional arguments to be passed to the resolution process.

        Returns:
            tuple: A tuple containing:
                - list: A list of resolved domain names (in default dnspython answer format).
                - list: A list of errors encountered during resolution.

        Examples:
            >>> results, errors = await _resolve_ip("8.8.8.8")
            (<dns.resolver.Answer object at 0x7f4a47cdb1d0>, [])
        """
        self.debug(f"Reverse-resolving {query} with kwargs={kwargs}")
        retries = kwargs.pop("retries", 0)
        use_cache = kwargs.pop("use_cache", True)
        tries_left = int(retries) + 1
        results = []
        errors = []
        dns_cache_hash = hash(f"{query}:PTR")
        while tries_left > 0:
            try:
                if use_cache:
                    results = self._dns_cache.get(dns_cache_hash, [])
                if not results:
                    results = await self._catch(self.resolver.resolve_address, query, **kwargs)
                    if use_cache:
                        self._dns_cache[dns_cache_hash] = results
                break
            except (
                dns.exception.Timeout,
                dns.resolver.LifetimeTimeout,
                dns.resolver.NoNameservers,
                TimeoutError,
            ) as e:
                errors.append(e)
                # don't retry if we get a SERVFAIL
                if isinstance(e, dns.resolver.NoNameservers):
                    self.debug(f"{e} (query={query}, kwargs={kwargs})")
                    break
                else:
                    tries_left -= 1
                    if tries_left > 0:
                        retry_num = (retries + 2) - tries_left
                        self.debug(f"Retrying (#{retry_num}) {query} with kwargs={kwargs}")

        if results:
            self._last_dns_success = time.time()

        return results, errors

    async def handle_wildcard_event(self, event, children):
        """
        Used within BBOT's scan manager to detect and tag DNS wildcard events.

        Wildcards are detected for every major record type. If a wildcard is detected, its data
        is overwritten, for example: `_wildcard.evilcorp.com`.

        Args:
            event (object): The event to check for wildcards.
            children (list): A list of the event's resulting DNS children after resolution.

        Returns:
            None: This method modifies the `event` in place and does not return a value.

        Examples:
            >>> handle_wildcard_event(event, children)
            # The `event` might now have tags like ["wildcard", "a-wildcard", "aaaa-wildcard"] and
            # its `data` attribute might be modified to "_wildcard.evilcorp.com" if it was detected
            # as a wildcard.
        """
        log.debug(f"Entering handle_wildcard_event({event}, children={children})")
        try:
            event_host = str(event.host)
            event_is_ip = is_ip(event_host)
            # wildcard checks
            if not event_is_ip:
                # check if the dns name itself is a wildcard entry
                wildcard_rdtypes = await self.is_wildcard(event_host)
                for rdtype, (is_wildcard, wildcard_host) in wildcard_rdtypes.items():
                    wildcard_tag = "error"
                    if is_wildcard == True:
                        event.add_tag("wildcard")
                        wildcard_tag = "wildcard"
                    event.add_tag(f"{rdtype.lower()}-{wildcard_tag}")

            # wildcard event modification (www.evilcorp.com --> _wildcard.evilcorp.com)
            if not event_is_ip and children:
                if wildcard_rdtypes:
                    # these are the rdtypes that successfully resolve
                    resolved_rdtypes = set([c.upper() for c in children])
                    # these are the rdtypes that have wildcards
                    wildcard_rdtypes_set = set(wildcard_rdtypes)
                    # consider the event a full wildcard if all its records are wildcards
                    event_is_wildcard = False
                    if resolved_rdtypes:
                        event_is_wildcard = all(r in wildcard_rdtypes_set for r in resolved_rdtypes)

                    if event_is_wildcard:
                        if event.type in ("DNS_NAME",) and not "_wildcard" in event.data.split("."):
                            wildcard_parent = parent_domain(event_host)
                            for rdtype, (_is_wildcard, _parent_domain) in wildcard_rdtypes.items():
                                if _is_wildcard:
                                    wildcard_parent = _parent_domain
                                    break
                            wildcard_data = f"_wildcard.{wildcard_parent}"
                            if wildcard_data != event.data:
                                log.debug(
                                    f'Wildcard detected, changing event.data "{event.data}" --> "{wildcard_data}"'
                                )
                                event.data = wildcard_data
                # tag wildcard domains for convenience
                elif is_domain(event_host) or hash(event_host) in self._wildcard_cache:
                    event_target = "target" in event.tags
                    wildcard_domain_results = await self.is_wildcard_domain(event_host, log_info=event_target)
                    for hostname, wildcard_domain_rdtypes in wildcard_domain_results.items():
                        if wildcard_domain_rdtypes:
                            event.add_tag("wildcard-domain")
                            for rdtype, ips in wildcard_domain_rdtypes.items():
                                event.add_tag(f"{rdtype.lower()}-wildcard-domain")
        finally:
            log.debug(f"Finished handle_wildcard_event({event}, children={children})")

    async def resolve_event(self, event_host, event_type, minimal=False):
        """
        Tag the given event with the appropriate DNS record types and optionally create child
        events based on DNS resolutions.

        Args:
            event (object): The event to be resolved and tagged.
            minimal (bool, optional): If set to True, the function will perform minimal DNS
                resolution. Defaults to False.

        Returns:
            tuple: A 4-tuple containing the following items:
                - event_tags (set): Set of tags for the event.
                - dns_children (dict): Dictionary containing child events from DNS resolutions.

        Examples:
            >>> event = make_event("evilcorp.com")
            >>> resolve_event(event)
            ({'resolved', 'ns-record', 'a-record',}, False, False, {'A': {IPv4Address('1.2.3.4'), IPv4Address('1.2.3.5')}, 'NS': {'ns1.evilcorp.com'}})

        Note:
            This method does not modify the passed in `event`. Instead, it returns data
            that can be used to modify or act upon the `event`.
        """
        log.debug(f"Resolving event {event_type}:{event_host}")
        event_tags = set()
        dns_children = dict()

        try:
            types = ()
            if is_ip(event_host):
                if not minimal:
                    types = ("PTR",)
            else:
                if event_type == "DNS_NAME" and not minimal:
                    types = self.all_rdtypes
                else:
                    types = ("A", "AAAA")
            queries = [(event_host, t) for t in types]
            async for (query, rdtype), (answers, errors) in self.resolve_raw_batch(queries):
                if answers:
                    rdtype = str(rdtype).upper()
                    event_tags.add("resolved")
                    event_tags.add(f"{rdtype.lower()}-record")

                    for host, _rdtype in answers:
                        if host:
                            host = make_ip_type(host)

                            if self.filter_bad_ptrs and rdtype in ("PTR") and is_ptr(host):
                                self.debug(f"Filtering out bad PTR: {host}")
                                continue

                            try:
                                dns_children[_rdtype].add(host)
                            except KeyError:
                                dns_children[_rdtype] = {host}

                elif errors:
                    event_tags.add(f"{rdtype.lower()}-error")

            # tag with cloud providers
            if not self.in_tests:
                to_check = set()
                if event_type == "IP_ADDRESS":
                    to_check.add(event_host)
                for rdtype, ips in dns_children.items():
                    if rdtype in ("A", "AAAA"):
                        for ip in ips:
                            to_check.add(ip)
                for ip in to_check:
                    provider, provider_type, subnet = cloudcheck(ip)
                    if provider:
                        event_tags.add(f"{provider_type}-{provider}")

            # if needed, mark as unresolved
            if not is_ip(event_host) and "resolved" not in event_tags:
                event_tags.add("unresolved")
            # check for private IPs
            for rdtype, ips in dns_children.items():
                for ip in ips:
                    try:
                        ip = ipaddress.ip_address(ip)
                        if ip.is_private:
                            event_tags.add("private-ip")
                    except ValueError:
                        continue

            return event_tags, dns_children

        finally:
            log.debug(f"Finished resolving event {event_type}:{event_host}")

    def event_cache_get(self, host):
        """
        Retrieves cached event data based on the given host.

        Args:
            host (str): The host for which the event data is to be retrieved.

        Returns:
            tuple: A 4-tuple containing the following items:
                - event_tags (set): Set of tags for the event.
                - dns_children (set): Set containing child events from DNS resolutions.

        Examples:
            Assuming an event with host "www.evilcorp.com" has been cached:

            >>> event_cache_get("www.evilcorp.com")
            ({"resolved", "a-record"}, False, False, {'1.2.3.4'})

            Assuming no event with host "www.notincache.com" has been cached:

            >>> event_cache_get("www.notincache.com")
            (set(), set())
        """
        try:
            event_tags, dns_children = self._event_cache[host]
            return (event_tags, dns_children)
        except KeyError:
            return set(), set()

    async def resolve_batch(self, queries, threads=10, **kwargs):
        """
        A helper to execute a bunch of DNS requests.

        Args:
            queries (list): List of queries to resolve.
            **kwargs: Additional keyword arguments to pass to `resolve()`.

        Yields:
            tuple: A tuple containing the original query and its resolved value.

        Examples:
            >>> import asyncio
            >>> async def example_usage():
            ...     async for result in resolve_batch(['www.evilcorp.com', 'evilcorp.com']):
            ...         print(result)
            ('www.evilcorp.com', {'1.1.1.1'})
            ('evilcorp.com', {'2.2.2.2'})
        """
        tasks = {}

        def new_task(query):
            task = asyncio.create_task(self.resolve(query, **kwargs))
            tasks[task] = query

        queries = list(queries)
        for _ in range(threads):  # Start initial batch of tasks
            if queries:  # Ensure there are args to process
                new_task(queries.pop(0))

        while tasks:  # While there are tasks pending
            # Wait for the first task to complete
            done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)

            for task in done:
                results = task.result()
                query = tasks.pop(task)

                if results:
                    yield (query, results)

                if queries:  # Start a new task for each one completed, if URLs remain
                    new_task(queries.pop(0))

    async def resolve_raw_batch(self, queries, threads=10):
        tasks = {}

        def new_task(query, rdtype):
            task = asyncio.create_task(self.resolve_raw(query, type=rdtype))
            tasks[task] = (query, rdtype)

        queries = list(queries)
        for _ in range(threads):  # Start initial batch of tasks
            if queries:  # Ensure there are args to process
                new_task(*queries.pop(0))

        while tasks:  # While there are tasks pending
            # Wait for the first task to complete
            done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)

            for task in done:
                answers, errors = task.result()
                query, rdtype = tasks.pop(task)

                results = set()
                for answer in answers:
                    for rdtype, host in self.extract_targets(answer):
                        results.add((host, rdtype))
                if results or errors:
                    yield ((query, rdtype), (results, errors))

                if queries:  # Start a new task for each one completed, if URLs remain
                    new_task(*queries.pop(0))

    def extract_targets(self, record):
        """
        Extracts hostnames or IP addresses from a given DNS record.

        This method reads the DNS record's type and based on that, extracts the target
        hostnames or IP addresses it points to. The type of DNS record
        (e.g., "A", "MX", "CNAME", etc.) determines which fields are used for extraction.

        Args:
            record (dns.rdata.Rdata): The DNS record to extract information from.

        Returns:
            set: A set of tuples, each containing the DNS record type and the extracted value.

        Examples:
            >>> from dns.rrset import from_text
            >>> record = from_text('www.example.com', 3600, 'IN', 'A', '192.0.2.1')
            >>> extract_targets(record[0])
            {('A', '192.0.2.1')}

            >>> record = from_text('example.com', 3600, 'IN', 'MX', '10 mail.example.com.')
            >>> extract_targets(record[0])
            {('MX', 'mail.example.com')}

        """
        results = set()
        rdtype = str(record.rdtype.name).upper()
        if rdtype in ("A", "AAAA", "NS", "CNAME", "PTR"):
            results.add((rdtype, clean_dns_record(record)))
        elif rdtype == "SOA":
            results.add((rdtype, clean_dns_record(record.mname)))
        elif rdtype == "MX":
            results.add((rdtype, clean_dns_record(record.exchange)))
        elif rdtype == "SRV":
            results.add((rdtype, clean_dns_record(record.target)))
        elif rdtype == "TXT":
            for s in record.strings:
                s = smart_decode(s)
                for match in dns_name_regex.finditer(s):
                    start, end = match.span()
                    host = s[start:end]
                    results.add((rdtype, host))
        elif rdtype == "NSEC":
            results.add((rdtype, clean_dns_record(record.next)))
        else:
            log.warning(f'Unknown DNS record type "{rdtype}"')
        return results

    async def _catch(self, callback, *args, **kwargs):
        """
        Asynchronously catches exceptions thrown during DNS resolution and logs them.

        This method wraps around a given asynchronous callback function to handle different
        types of DNS exceptions and general exceptions. It logs the exceptions for debugging
        and, in some cases, re-raises them.

        Args:
            callback (callable): The asynchronous function to be executed.
            *args: Positional arguments to pass to the callback.
            **kwargs: Keyword arguments to pass to the callback.

        Returns:
            Any: The return value of the callback function, or an empty list if an exception is caught.

        Raises:
            dns.resolver.NoNameservers: When no nameservers could be reached.
        """
        try:
            return await callback(*args, **kwargs)
        except dns.resolver.NoNameservers:
            raise
        except (dns.exception.Timeout, dns.resolver.LifetimeTimeout, TimeoutError):
            log.debug(f"DNS query with args={args}, kwargs={kwargs} timed out after {self.timeout} seconds")
            raise
        except dns.exception.DNSException as e:
            self.debug(f"{e} (args={args}, kwargs={kwargs})")
        except Exception as e:
            log.warning(f"Error in {callback.__qualname__}() with args={args}, kwargs={kwargs}: {e}")
            log.trace(traceback.format_exc())
        return []

    async def is_wildcard(self, query, ips=None, rdtype=None):
        """
        Use this method to check whether a *host* is a wildcard entry

        This can reliably tell the difference between a valid DNS record and a wildcard within a wildcard domain.

        If you want to know whether a domain is using wildcard DNS, use `is_wildcard_domain()` instead.

        Args:
            query (str): The hostname to check for a wildcard entry.
            ips (list, optional): List of IPs to compare against, typically obtained from a previous DNS resolution of the query.
            rdtype (str, optional): The DNS record type (e.g., "A", "AAAA") to consider during the check.

        Returns:
            dict: A dictionary indicating if the query is a wildcard for each checked DNS record type.
                Keys are DNS record types like "A", "AAAA", etc.
                Values are tuples where the first element is a boolean indicating if the query is a wildcard,
                and the second element is the wildcard parent if it's a wildcard.

        Raises:
            ValueError: If only one of `ips` or `rdtype` is specified or if no valid IPs are specified.

        Examples:
            >>> is_wildcard("www.github.io")
            {"A": (True, "github.io"), "AAAA": (True, "github.io")}

            >>> is_wildcard("www.evilcorp.com", ips=["93.184.216.34"], rdtype="A")
            {"A": (False, "evilcorp.com")}

        Note:
            `is_wildcard` can be True, False, or None (indicating that wildcard detection was inconclusive)
        """
        result = {}

        parent = parent_domain(query)
        parents = list(domain_parents(query))

        rdtypes_to_check = [rdtype] if rdtype is not None else self.all_rdtypes

        query_baseline = dict()
        # if the caller hasn't already done the work of resolving the IPs
        if ips is None:
            # then resolve the query for all rdtypes
            queries = [(query, t) for t in rdtypes_to_check]
            async for (query, _rdtype), (answers, errors) in self.resolve_raw_batch(queries):
                if answers:
                    query_baseline[_rdtype] = set([a[0] for a in answers])
                else:
                    if errors:
                        self.debug(f"Failed to resolve {query} ({_rdtype}) during wildcard detection")
                        result[_rdtype] = (None, parent)
                        continue
        else:
            # otherwise, we can skip all that
            cleaned_ips = set([clean_dns_record(ip) for ip in ips])
            if not cleaned_ips:
                raise ValueError("Valid IPs must be specified")
            query_baseline[rdtype] = cleaned_ips
        if not query_baseline:
            return result

        # once we've resolved the base query and have IP addresses to work with
        # we can compare the IPs to the ones we have on file for wildcards

        # for every parent domain, starting with the shortest
        try:
            for host in parents[::-1]:
                # make sure we've checked that domain for wildcards
                await self.is_wildcard_domain(host)

                # for every rdtype
                for _rdtype in list(query_baseline):
                    # get the IPs from above
                    query_ips = query_baseline.get(_rdtype, set())
                    host_hash = hash(host)

                    if host_hash in self._wildcard_cache:
                        # then get its IPs from our wildcard cache
                        wildcard_rdtypes = self._wildcard_cache[host_hash]

                        # then check to see if our IPs match the wildcard ones
                        if _rdtype in wildcard_rdtypes:
                            wildcard_ips = wildcard_rdtypes[_rdtype]
                            # if our IPs match the wildcard ones, then ladies and gentlemen we have a wildcard
                            is_wildcard = any(r in wildcard_ips for r in query_ips)

                            if is_wildcard and not result.get(_rdtype, (None, None))[0] is True:
                                result[_rdtype] = (True, host)

                    # if we've reached a point where the dns name is a complete wildcard, class can be dismissed early
                    base_query_rdtypes = set(query_baseline)
                    wildcard_rdtypes_set = set([k for k, v in result.items() if v[0] is True])
                    if base_query_rdtypes and wildcard_rdtypes_set and base_query_rdtypes == wildcard_rdtypes_set:
                        log.debug(
                            f"Breaking from wildcard detection for {query} at {host} because base query rdtypes ({base_query_rdtypes}) == wildcard rdtypes ({wildcard_rdtypes_set})"
                        )
                        raise DNSWildcardBreak()

        except DNSWildcardBreak:
            pass

        return result

    async def is_wildcard_domain(self, domain, log_info=False):
        """
        Check whether a given host or its children make use of wildcard DNS entries. Wildcard DNS can have
        various implications, particularly in subdomain enumeration and subdomain takeovers.

        Args:
            domain (str): The domain to check for wildcard DNS entries.
            log_info (bool, optional): Whether to log the result of the check. Defaults to False.

        Returns:
            dict: A dictionary where the keys are the parent domains that have wildcard DNS entries,
            and the values are another dictionary of DNS record types ("A", "AAAA", etc.) mapped to
            sets of their resolved IP addresses.

        Examples:
            >>> is_wildcard_domain("github.io")
            {"github.io": {"A": {"1.2.3.4"}, "AAAA": {"dead::beef"}}}

            >>> is_wildcard_domain("example.com")
            {}
        """
        wildcard_domain_results = {}
        domain = clean_dns_record(domain)

        if not is_dns_name(domain):
            return {}

        # skip check if the query's parent domain is excluded in the config
        for d in self.wildcard_ignore:
            if host_in_host(domain, d):
                log.debug(f"Skipping wildcard detection on {domain} because it is excluded in the config")
                return {}

        rdtypes_to_check = set(self.all_rdtypes)

        # make a list of its parents
        parents = list(domain_parents(domain, include_self=True))
        # and check each of them, beginning with the highest parent (i.e. the root domain)
        for i, host in enumerate(parents[::-1]):
            # have we checked this host before?
            host_hash = hash(host)
            async with self._wildcard_lock.lock(host_hash):
                # if we've seen this host before
                if host_hash in self._wildcard_cache:
                    wildcard_domain_results[host] = self._wildcard_cache[host_hash]
                    continue

                log.verbose(f"Checking if {host} is a wildcard")

                # determine if this is a wildcard domain

                # resolve a bunch of random subdomains of the same parent
                is_wildcard = False
                wildcard_results = dict()

                queries = []
                for rdtype in list(rdtypes_to_check):
                    for _ in range(self.wildcard_tests):
                        rand_query = f"{rand_string(digits=False, length=10)}.{host}"
                        queries.append((rand_query, rdtype))

                async for (query, rdtype), (answers, errors) in self.resolve_raw_batch(queries):
                    if answers:
                        is_wildcard = True
                        if not rdtype in wildcard_results:
                            wildcard_results[rdtype] = set()
                        wildcard_results[rdtype].update(set(a[0] for a in answers))
                        # we know this rdtype is a wildcard
                        # so we don't need to check it anymore
                        with suppress(KeyError):
                            rdtypes_to_check.remove(rdtype)

                self._wildcard_cache.update({host_hash: wildcard_results})
                wildcard_domain_results.update({host: wildcard_results})
                if is_wildcard:
                    wildcard_rdtypes_str = ",".join(sorted([t.upper() for t, r in wildcard_results.items() if r]))
                    log_fn = log.verbose
                    if log_info:
                        log_fn = log.info
                    log_fn(f"Encountered domain with wildcard DNS ({wildcard_rdtypes_str}): {host}")
                else:
                    log.verbose(f"Finished checking {host}, it is not a wildcard")

        return wildcard_domain_results

    async def _connectivity_check(self, interval=5):
        """
        Periodically checks for an active internet connection by attempting DNS resolution.

        Args:
            interval (int, optional): The time interval, in seconds, at which to perform the check.
            Defaults to 5 seconds.

        Returns:
            bool: True if there is an active internet connection, False otherwise.

        Examples:
            >>> await _connectivity_check()
            True
        """
        if self._last_dns_success is not None:
            if time.time() - self._last_dns_success < interval:
                return True
        dns_server_working = []
        async with self._dns_connectivity_lock:
            with suppress(Exception):
                dns_server_working = await self._catch(self.resolver.resolve, "www.google.com", rdtype="A")
                if dns_server_working:
                    self._last_dns_success = time.time()
                    return True
        if time.time() - self._last_connectivity_warning > interval:
            log.warning(f"DNS queries are failing, please check your internet connection")
            self._last_connectivity_warning = time.time()
        self._errors.clear()
        return False

    def _parse_rdtype(self, t, default=None):
        if isinstance(t, str):
            if t.strip().lower() in ("any", "all", "*"):
                return self.all_rdtypes
            else:
                return [t.strip().upper()]
        elif any([isinstance(t, x) for x in (list, tuple)]):
            return [str(_).strip().upper() for _ in t]
        return default

    def debug(self, *args, **kwargs):
        if self._debug:
            log.trace(*args, **kwargs)

    @property
    def in_tests(self):
        return os.getenv("BBOT_TESTING", "") == "True"

    async def _mock_dns(self, mock_data):
        from .mock import MockResolver

        self.resolver = MockResolver(mock_data)
