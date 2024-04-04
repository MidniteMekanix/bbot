from ..bbot_fixtures import *


@pytest.mark.asyncio
async def test_dns_engine(bbot_scanner):
    scan = bbot_scanner()
    result = await scan.helpers.resolve("one.one.one.one")
    assert "1.1.1.1" in result
    assert not "2606:4700:4700::1111" in result

    results = [_ async for _ in scan.helpers.resolve_batch(("one.one.one.one", "1.1.1.1"))]
    pass_1 = False
    pass_2 = False
    for query, result in results:
        if query == "one.one.one.one" and "1.1.1.1" in result:
            pass_1 = True
        elif query == "1.1.1.1" and "one.one.one.one" in result:
            pass_2 = True
    assert pass_1 and pass_2

    results = [_ async for _ in scan.helpers.resolve_raw_batch((("one.one.one.one", "A"), ("1.1.1.1", "PTR")))]
    pass_1 = False
    pass_2 = False
    for (query, rdtype), (result, errors) in results:
        _results = [r[0] for r in result]
        if query == "one.one.one.one" and "1.1.1.1" in _results:
            pass_1 = True
        elif query == "1.1.1.1" and "one.one.one.one" in _results:
            pass_2 = True
    assert pass_1 and pass_2


@pytest.mark.asyncio
async def test_dns(bbot_scanner):
    scan = bbot_scanner("1.1.1.1")

    from bbot.core.helpers.dns.engine import DNSEngine

    dnsengine = DNSEngine(None)

    # lowest level functions
    a_responses = await dnsengine._resolve_hostname("one.one.one.one")
    aaaa_responses = await dnsengine._resolve_hostname("one.one.one.one", rdtype="AAAA")
    ip_responses = await dnsengine._resolve_ip("1.1.1.1")
    assert a_responses[0].response.answer[0][0].address in ("1.1.1.1", "1.0.0.1")
    assert aaaa_responses[0].response.answer[0][0].address in ("2606:4700:4700::1111", "2606:4700:4700::1001")
    assert ip_responses[0].response.answer[0][0].target.to_text() in ("one.one.one.one.",)

    # mid level functions
    answers, errors = await dnsengine.resolve_raw("one.one.one.one", type="A")
    responses = []
    for answer in answers:
        responses += list(dnsengine.extract_targets(answer))
    assert ("A", "1.1.1.1") in responses
    assert not ("AAAA", "2606:4700:4700::1111") in responses
    answers, errors = await dnsengine.resolve_raw("one.one.one.one", type="AAAA")
    responses = []
    for answer in answers:
        responses += list(dnsengine.extract_targets(answer))
    assert not ("A", "1.1.1.1") in responses
    assert ("AAAA", "2606:4700:4700::1111") in responses
    answers, errors = await dnsengine.resolve_raw("1.1.1.1")
    responses = []
    for answer in answers:
        responses += list(dnsengine.extract_targets(answer))
    assert ("PTR", "one.one.one.one") in responses

    # high level functions
    assert "1.1.1.1" in await dnsengine.resolve("one.one.one.one")
    assert "2606:4700:4700::1111" in await dnsengine.resolve("one.one.one.one", type="AAAA")
    assert "one.one.one.one" in await dnsengine.resolve("1.1.1.1")
    for rdtype in ("NS", "SOA", "MX", "TXT"):
        assert len(await dnsengine.resolve("google.com", type=rdtype)) > 0

    # batch resolution
    batch_results = [r async for r in dnsengine.resolve_batch(["1.1.1.1", "one.one.one.one"])]
    assert len(batch_results) == 2
    batch_results = dict(batch_results)
    assert any([x in batch_results["one.one.one.one"] for x in ("1.1.1.1", "1.0.0.1")])
    assert "one.one.one.one" in batch_results["1.1.1.1"]

    # custom batch resolution
    batch_results = [r async for r in dnsengine.resolve_raw_batch([("1.1.1.1", "PTR"), ("one.one.one.one", "A")])]
    assert len(batch_results) == 2
    batch_results = dict(batch_results)
    assert ("1.1.1.1", "A") in batch_results[("one.one.one.one", "A")][0]
    assert ("one.one.one.one", "PTR") in batch_results[("1.1.1.1", "PTR")][0]

    # dns cache
    dnsengine._dns_cache.clear()
    assert hash(f"1.1.1.1:PTR") not in dnsengine._dns_cache
    assert hash(f"one.one.one.one:A") not in dnsengine._dns_cache
    assert hash(f"one.one.one.one:AAAA") not in dnsengine._dns_cache
    await dnsengine.resolve("1.1.1.1", use_cache=False)
    await dnsengine.resolve("one.one.one.one", use_cache=False)
    assert hash(f"1.1.1.1:PTR") not in dnsengine._dns_cache
    assert hash(f"one.one.one.one:A") not in dnsengine._dns_cache
    assert hash(f"one.one.one.one:AAAA") not in dnsengine._dns_cache

    await dnsengine.resolve("1.1.1.1")
    assert hash(f"1.1.1.1:PTR") in dnsengine._dns_cache
    await dnsengine.resolve("one.one.one.one", type="A")
    assert hash(f"one.one.one.one:A") in dnsengine._dns_cache
    assert not hash(f"one.one.one.one:AAAA") in dnsengine._dns_cache
    dnsengine._dns_cache.clear()
    await dnsengine.resolve("one.one.one.one", type="AAAA")
    assert hash(f"one.one.one.one:AAAA") in dnsengine._dns_cache
    assert not hash(f"one.one.one.one:A") in dnsengine._dns_cache

    # Ensure events with hosts have resolved_hosts attribute populated
    resolved_hosts_event1 = scan.make_event("one.one.one.one", "DNS_NAME", dummy=True)
    resolved_hosts_event2 = scan.make_event("http://one.one.one.one/", "URL_UNVERIFIED", dummy=True)
    assert resolved_hosts_event1.host not in scan.helpers.dns._event_cache
    assert resolved_hosts_event2.host not in scan.helpers.dns._event_cache
    event_tags1, event_whitelisted1, event_blacklisted1, children1 = await scan.helpers.resolve_event(
        resolved_hosts_event1
    )
    assert resolved_hosts_event1.host in scan.helpers.dns._event_cache
    assert resolved_hosts_event2.host in scan.helpers.dns._event_cache
    event_tags2, event_whitelisted2, event_blacklisted2, children2 = await scan.helpers.resolve_event(
        resolved_hosts_event2
    )
    assert "1.1.1.1" in [str(x) for x in children1["A"]]
    assert "1.1.1.1" in [str(x) for x in children2["A"]]
    assert set(children1.keys()) == set(children2.keys())

    scan2 = bbot_scanner("evilcorp.com", config={"dns_resolution": True})
    await scan2.helpers.dns._mock_dns(
        {
            "evilcorp.com": {"TXT": ['"v=spf1 include:cloudprovider.com ~all"']},
            "cloudprovider.com": {"A": ["1.2.3.4"]},
        },
    )
    events = [e async for e in scan2.async_start()]
    assert 1 == len(
        [e for e in events if e.type == "DNS_NAME" and e.data == "cloudprovider.com" and "affiliate" in e.tags]
    )


@pytest.mark.asyncio
async def test_wildcards(bbot_scanner):
    scan = bbot_scanner("1.1.1.1")
    helpers = scan.helpers

    from bbot.core.helpers.dns.engine import DNSEngine

    dnsengine = DNSEngine(None)

    # wildcards
    wildcard_domains = await dnsengine.is_wildcard_domain("asdf.github.io")
    assert hash("github.io") in dnsengine._wildcard_cache
    assert hash("asdf.github.io") in dnsengine._wildcard_cache
    assert "github.io" in wildcard_domains
    assert "A" in wildcard_domains["github.io"]
    assert "SRV" not in wildcard_domains["github.io"]
    assert wildcard_domains["github.io"]["A"] and all(helpers.is_ip(r) for r in wildcard_domains["github.io"]["A"])
    dnsengine._wildcard_cache.clear()

    wildcard_rdtypes = await dnsengine.is_wildcard("blacklanternsecurity.github.io")
    assert "A" in wildcard_rdtypes
    assert "SRV" not in wildcard_rdtypes
    assert wildcard_rdtypes["A"] == (True, "github.io")
    assert hash("github.io") in dnsengine._wildcard_cache
    assert len(dnsengine._wildcard_cache[hash("github.io")]) > 0
    dnsengine._wildcard_cache.clear()

    wildcard_rdtypes = await dnsengine.is_wildcard("asdf.asdf.asdf.github.io")
    assert "A" in wildcard_rdtypes
    assert "SRV" not in wildcard_rdtypes
    assert wildcard_rdtypes["A"] == (True, "github.io")
    assert hash("github.io") in dnsengine._wildcard_cache
    assert not hash("asdf.github.io") in dnsengine._wildcard_cache
    assert not hash("asdf.asdf.github.io") in dnsengine._wildcard_cache
    assert not hash("asdf.asdf.asdf.github.io") in dnsengine._wildcard_cache
    assert len(dnsengine._wildcard_cache[hash("github.io")]) > 0
    wildcard_event1 = scan.make_event("wat.asdf.fdsa.github.io", "DNS_NAME", dummy=True)
    wildcard_event2 = scan.make_event("wats.asd.fdsa.github.io", "DNS_NAME", dummy=True)
    wildcard_event3 = scan.make_event("github.io", "DNS_NAME", dummy=True)

    # event resolution
    event_tags1, event_whitelisted1, event_blacklisted1, children1 = await scan.helpers.resolve_event(wildcard_event1)
    event_tags2, event_whitelisted2, event_blacklisted2, children2 = await scan.helpers.resolve_event(wildcard_event2)
    event_tags3, event_whitelisted3, event_blacklisted3, children3 = await scan.helpers.resolve_event(wildcard_event3)
    await helpers.handle_wildcard_event(wildcard_event1, children1)
    await helpers.handle_wildcard_event(wildcard_event2, children2)
    await helpers.handle_wildcard_event(wildcard_event3, children3)
    assert "wildcard" in wildcard_event1.tags
    assert "a-wildcard" in wildcard_event1.tags
    assert "srv-wildcard" not in wildcard_event1.tags
    assert "wildcard" in wildcard_event2.tags
    assert "a-wildcard" in wildcard_event2.tags
    assert "srv-wildcard" not in wildcard_event2.tags
    assert wildcard_event1.data == "_wildcard.github.io"
    assert wildcard_event2.data == "_wildcard.github.io"
    # TODO: re-enable this?
    # assert "wildcard-domain" in wildcard_event3.tags
    # assert "a-wildcard-domain" in wildcard_event3.tags
    # assert "srv-wildcard-domain" not in wildcard_event3.tags
