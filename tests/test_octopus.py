"""Drive the source with faked API replies, shaped like the real ones.

    python tests/test_octopus.py

Every reply here is the shape `api.octopus.energy` really answers with, down to the unit
rates arriving newest first, and all of them are faked in this file.
"""
import datetime
import sys

from statsbadge_octopus import (
    FORWARD_SLOTS,
    GAS_M3_TO_KWH,
    HISTORY_SLOTS,
    SLOT_S,
    Octopus,
    _cost,
    _curved,
    _current_tariff,
    _last_full_day,
    _priced,
    _product_of,
    _slot_start,
)

CHECKS = []
NOW = datetime.datetime.now(datetime.timezone.utc)
ELEC = "octopus_elec_5967"
GAS = "octopus_gas_3210"

# Agile really does go below zero when the grid is oversupplied.
AGILE_PRICES = [22.5, 18.9, 14.2, -1.4, 9.8, 11.0, 24.6, 31.2, 28.0, 15.5, 12.1, 10.0]


def check(fn):
    CHECKS.append(fn)
    return fn


def slot(offset):
    return _slot_start(NOW) + datetime.timedelta(minutes=30 * offset)


def stamp(when):
    return when.strftime("%Y-%m-%dT%H:%M:%SZ")


ACCOUNT = {
    "number": "A-1234ABCD",
    "properties": [{
        "electricity_meter_points": [
            {"mpan": "1200023305967", "is_export": False,
             # Two of them: an exchanged meter, then the live one.
             "meters": [{"serial_number": "17L0000001"}, {"serial_number": "19L3255555"}],
             "agreements": [
                 {"tariff_code": "E-1R-VAR-22-11-01-M",
                  "valid_from": "2023-01-01T00:00:00Z", "valid_to": "2024-10-01T00:00:00Z"},
                 {"tariff_code": "E-1R-AGILE-24-10-01-M",
                  "valid_from": "2024-10-01T00:00:00Z", "valid_to": None},
             ]},
            {"mpan": "1900012345678", "is_export": True,
             "meters": [{"serial_number": "20L9999999"}],
             "agreements": [{"tariff_code": "E-1R-OUTGOING-FIX-12M-19-05-13-M",
                             "valid_from": "2024-01-01T00:00:00Z", "valid_to": None}]},
        ],
        "gas_meter_points": [
            {"mprn": "9876543210",
             "meters": [{"serial_number": "G4K1112222"}],
             "agreements": [{"tariff_code": "G-1R-VAR-22-11-01-M",
                             "valid_from": "2024-01-01T00:00:00Z", "valid_to": None}]},
        ],
    }],
}


def agile_rates():
    """Two days behind and twelve slots ahead, newest first, as the endpoint answers."""
    rows = [{"value_inc_vat": AGILE_PRICES[step % len(AGILE_PRICES)],
             "value_exc_vat": AGILE_PRICES[step % len(AGILE_PRICES)] / 1.05,
             "valid_from": stamp(slot(step)), "valid_to": stamp(slot(step + 1))}
            for step in range(-96, FORWARD_SLOTS)]
    rows.reverse()
    return {"count": len(rows), "next": None, "previous": None, "results": rows}


def flat_rates(price):
    """A fixed tariff: one row, open ended."""
    return {"results": [{"value_inc_vat": price, "value_exc_vat": price / 1.05,
                         "valid_from": "2024-01-01T00:00:00Z", "valid_to": None}]}


def consumption(each, days=2):
    """Half-hours running back from the last complete local midnight."""
    end = _slot_start(NOW).astimezone().replace(hour=0, minute=0).astimezone(
        datetime.timezone.utc)
    when, rows = end - datetime.timedelta(days=days), []
    while when < end:
        rows.append({"consumption": each, "interval_start": stamp(when),
                     "interval_end": stamp(when + datetime.timedelta(minutes=30))})
        when += datetime.timedelta(minutes=30)
    return {"results": rows}


class Faked(Octopus):
    """The source with its one network call replaced, and a record of what it asked for."""

    # An empty config is a source nobody has configured, which is a case worth testing, so
    # this cannot be `config or {...}`.
    CONFIGURED = {"api_key": "sk_live_fake", "account_number": "A-1234ABCD",
                  "gas_units": "m3"}

    def __init__(self, config=None):
        self.asked = []
        super().__init__(dict(self.CONFIGURED) if config is None else config)

    def _get(self, path):
        self.asked.append(path.split("?")[0])
        if path.startswith("/accounts/"):
            return ACCOUNT
        if "standing-charges" in path:
            return {"results": [{"value_inc_vat": 31.4 if "electricity" in path else 27.2,
                                 "valid_from": "2024-01-01T00:00:00Z", "valid_to": None}]}
        if "standard-unit-rates" in path:
            if "AGILE" in path:
                return agile_rates()
            return flat_rates(6.4 if "/gas-tariffs/" in path else 24.1)
        if "/consumption/" in path:
            # The exchanged meter answers with an empty list, as a real one does.
            if "17L0000001" in path:
                return {"results": []}
            # A gas meter reporting cubic metres, and electricity in kWh.
            return consumption(0.25 if "/electricity-" in path else 0.05)
        raise AssertionError(f"unexpected path {path}")


def fetched():
    """A source with one of every cycle run, which is what a first poll does."""
    source = Faked()
    source._refresh_account()
    source._refresh_rates()
    source._refresh_standing()
    source._refresh_use()
    return source


@check
def test_a_tariff_code_names_its_product():
    """The rate endpoints are keyed by product and the account only names a tariff."""
    assert _product_of("E-1R-AGILE-24-10-01-M") == "AGILE-24-10-01"
    assert _product_of("G-1R-VAR-22-11-01-M") == "VAR-22-11-01"
    assert _product_of("E-2R-VAR-22-11-01-A") == "VAR-22-11-01"
    # Too short to take the ends off answers with no product, and never a wrong one.
    assert _product_of("") == ""
    assert _product_of("E-1R-M") == ""


@check
def test_the_tariff_in_force_beats_the_one_that_was():
    """An account carries every agreement it has ever had, and tomorrow's beside today's."""
    agreements = ACCOUNT["properties"][0]["electricity_meter_points"][0]["agreements"]
    assert _current_tariff(agreements) == "E-1R-AGILE-24-10-01-M"
    # A switch that starts tomorrow leaves today's agreement running.
    ahead = [*agreements, {"tariff_code": "E-1R-GO-24-01-01-M",
                           "valid_from": stamp(NOW + datetime.timedelta(days=1)),
                           "valid_to": None}]
    assert _current_tariff(ahead) == "E-1R-AGILE-24-10-01-M"
    # An account between agreements falls back to the latest, so the page still prices.
    assert _current_tariff([{"tariff_code": "E-1R-VAR-22-11-01-M",
                             "valid_from": "2020-01-01T00:00:00Z",
                             "valid_to": "2021-01-01T00:00:00Z"}]) == "E-1R-VAR-22-11-01-M"
    assert _current_tariff(()) == ""


@check
def test_a_meter_point_is_named_by_its_fuel():
    """A graph of gas and electricity is told apart by which is which.

    The badge names a series by its field where that is unique and by the group where it is
    not, so two kWh series fall back to these. The tail of an MPAN told them apart and meant
    nothing to anybody reading the page.
    """
    source = Faked()
    source._refresh_account()
    labels = {point["fuel"]: point["label"] for point in source._points}
    assert labels == {"electricity": "Electricity", "gas": "Gas"}, labels
    # The group key still carries it: a layout names groups, and two accounts could hold a
    # meter each.
    assert all(point["id"][-4:] in point["group"] for point in source._points)

    # Two of one fuel is the only case with nothing else to go on.
    twin = dict(ACCOUNT)
    prop = dict(ACCOUNT["properties"][0])
    prop["gas_meter_points"] = [*prop["gas_meter_points"],
                                {"mprn": "1234500000",
                                 "meters": [{"serial_number": "G4K9999999"}],
                                 "agreements": [{"tariff_code": "G-1R-VAR-22-11-01-M",
                                                 "valid_from": "2024-01-01T00:00:00Z",
                                                 "valid_to": None}]}]
    twin["properties"] = [prop]

    class TwoMeters(Faked):
        def _get(self, path):
            if path.startswith("/accounts/"):
                return twin
            return super()._get(path)

    two = TwoMeters()
    two._refresh_account()
    named = sorted(point["label"] for point in two._points)
    assert named == ["Electricity", "Gas 0000", "Gas 3210"], named


@check
def test_an_export_meter_is_left_out():
    """What a panel sold and what the house bought would draw the same page."""
    source = Faked()
    source._refresh_account()
    assert [point["group"] for point in source._points] == [ELEC, GAS], source._points
    assert all("OUTGOING" not in point["tariff"] for point in source._points)


@check
def test_the_curve_ahead_is_bars_with_the_times_on_them():
    """The half of an Agile curve worth reading has not happened yet.

    `series()` cannot carry it - the collector clamps a ring's age to zero - so it travels
    as a list field with the names beside it.
    """
    source = fetched()
    frame = {}
    source.sample(frame, 1.0)
    values = frame[ELEC]

    assert len(values["forward_p"]) == FORWARD_SLOTS
    assert len(values["forward_p_names"]) == FORWARD_SLOTS
    # The price now is the slot in progress, which is the first bar.
    assert values["price"] == values["forward_p"][0]
    assert values["price_next"] == values["forward_p"][1]
    # Half past or on the hour, in local time, since the badge is read in the room.
    assert all(name[3:] in ("00", "30") for name in values["forward_p_names"]), values
    # A negative is carried rather than clamped.
    assert min(values["forward_p"]) < 0, values["forward_p"]
    assert values["price_min"] < 0


@check
def test_the_cheapest_slot_is_named_by_how_far_off_it_is():
    """"Cheapest in 1.5h" is the reading somebody acts on."""
    rates = [(slot(step), slot(step + 1), price)
             for step, price in enumerate([20.0, 18.0, 4.0, 25.0])]
    priced = _priced(rates, NOW)
    assert priced["price"] == 20.0
    assert priced["cheapest_in_h"] == 1.0, priced
    assert priced["price_min"] == 4.0 and priced["price_max"] == 25.0
    # Behind the slot in progress is the history ring's business, not this one's.
    assert len(priced["forward_p"]) == 4


@check
def test_a_fixed_tariff_gets_no_curve():
    """Twelve identical bars say less than one reading, and a graph of them is a line."""
    one = [(slot(-1000), slot(1000), 24.1)]
    priced = _priced(one, NOW)
    assert priced == {"price": 24.1}, priced

    source = fetched()
    assert "forward_p" in source.groups[ELEC]["fields"]
    assert "forward_p" not in source.groups[GAS]["fields"], source.groups[GAS]["fields"]
    frame = {}
    source.sample(frame, 1.0)
    assert "cheapest_in_h" not in frame[GAS], frame[GAS]
    assert frame[GAS]["price"] == 6.4
    # Which is why the product is a reading: a page with no curve is a tariff with none.
    assert frame[GAS]["tariff"] == "VAR-22-11-01", frame[GAS]
    assert frame[ELEC]["tariff"] == "AGILE-24-10-01", frame[ELEC]
    # No ring either: one row covering a year fills none of the slots anyway.
    assert f"{GAS}.price" not in source.series(), sorted(source.series())


@check
def test_a_curve_is_told_by_how_wide_its_rows_are():
    """Counting what is ahead dropped the fields every evening.

    Agile publishes only as far as 23:00 tomorrow, so by late evening a handful of slots are
    left; the tariff still has a curve. A fixed rate is one row until further notice.
    """
    agile = [(slot(step), slot(step + 1), 10.0) for step in range(-4, 2)]
    assert _curved(agile)
    # Two slots ahead is still Agile, and the page keeps its fields.
    priced = _priced(agile, NOW)
    assert "forward_p" in priced and len(priced["forward_p"]) == 2, priced

    assert not _curved([(slot(-1000), slot(1000), 24.1)])
    assert not _curved([])
    # A row exactly a settlement period wide counts, since that is what Agile sends.
    one = slot(0)
    assert _curved([(one, one + datetime.timedelta(seconds=SLOT_S), 5.0)])


@check
def test_a_price_the_slot_has_run_past_still_holds():
    """A tariff published no further than now is the price being paid, not a blank page."""
    behind = [(slot(-2), slot(-1), 19.0), (slot(-1), slot(1), 21.0)]
    assert _priced(behind, NOW) == {"price": 21.0}
    assert _priced((), NOW) == {}


@check
def test_each_meter_is_priced_against_its_own_tariff():
    """A house with gas holds two meter points on two products."""
    source = fetched()
    frame = {}
    source.sample(frame, 1.0)

    assert frame[ELEC]["standing_charge"] == 31.4
    assert frame[GAS]["standing_charge"] == 27.2
    # The gas day at the gas rate, and not at the electricity rate.
    kwh = 48 * 0.05 * GAS_M3_TO_KWH
    assert abs(frame[GAS]["cost_day"] - kwh * 6.4) < 1.0, frame[GAS]
    # An Agile day is priced slot by slot, so it lands inside the day's own range.
    day = frame[ELEC]["kwh_day"]
    assert min(AGILE_PRICES) * day <= frame[ELEC]["cost_day"] <= max(AGILE_PRICES) * day


@check
def test_every_meter_on_a_point_is_asked():
    """An electricity point lists the meters it has ever had after an exchange.

    The exchanged one answers with an empty list, and it is not always last, so taking the
    first serial gave a group with a price on it and no readings at all.
    """
    source = fetched()
    asked = [path for path in source.asked if path.endswith("/consumption/")]
    assert any("17L0000001" in path for path in asked), asked
    assert any("19L3255555" in path for path in asked), asked

    frame = {}
    source.sample(frame, 1.0)
    assert frame[ELEC]["kwh"] == 0.25, frame[ELEC]
    assert source.last_fault is None, source.last_fault


@check
def test_a_meter_that_has_reported_nothing_says_so():
    """A group with a price and no readings looks the same as a broken extension."""
    class Silent(Faked):
        def _get(self, path):
            if "/consumption/" in path:
                self.asked.append(path.split("?")[0])
                return {"results": []}
            return super()._get(path)

    source = Silent()
    source._refresh_account()
    source._refresh_rates()
    source._refresh_standing()
    source._refresh_use()
    assert source.last_fault and "reported nothing" in source.last_fault, source.last_fault
    # Both serials were tried before giving up on the point.
    tried = [p for p in source.asked if "/electricity-meter-points/" in p]
    assert len(tried) == 2, tried


@check
def test_a_part_reported_day_is_not_costed():
    """A day short of an hour reads as a quiet one, and its cost is short by as much."""
    # Three days back, so one whole local day is in there whatever time it is now.
    whole = [(slot(-step), 0.2) for step in range(1, 3 * 48)]
    day = _last_full_day(whole)
    assert day is not None and len(day) >= 48, None if day is None else len(day)
    # Today so far falls short of a day, however many hours of it there are.
    assert _last_full_day([(slot(-step), 0.2) for step in range(1, 10)]) is None

    # A slot with no price is no total: four fifths of a day reads as a cheap day.
    used = [(slot(-2), 1.0), (slot(-1), 1.0)]
    rates = [(slot(-2), slot(-1), 10.0)]
    assert _cost(used, rates) is None
    assert _cost(used, [*rates, (slot(-1), slot(0), 20.0)]) == 30.0
    # A fixed rate is one row covering the lot, so it prices every slot in it.
    assert _cost(used, [(slot(-1000), slot(1000), 5.0)]) == 10.0
    assert _cost([], rates) is None


@check
def test_gas_is_converted_out_of_cubic_metres():
    """Nothing in the API says which a meter reports, so it is a setting."""
    source = fetched()
    frame = {}
    source.sample(frame, 1.0)
    assert abs(frame[GAS]["kwh_day"] - 48 * 0.05 * GAS_M3_TO_KWH) < 0.05, frame[GAS]
    assert abs(frame[ELEC]["kwh_day"] - 48 * 0.25) < 0.01, frame[ELEC]

    told = Faked({"api_key": "k", "account_number": "A-1", "gas_units": "kWh"})
    told._refresh_account()
    told._refresh_rates()
    told._refresh_standing()
    told._refresh_use()
    plain = {}
    told.sample(plain, 1.0)
    assert abs(plain[GAS]["kwh_day"] - 48 * 0.05) < 0.01, plain[GAS]


@check
def test_no_meter_reading_claims_to_be_recent():
    """A meter 19 hours behind offering a reading called "last half hour" is a lie.

    Octopus has the half-hours a meter has sent it, and a meter sends them a day or two
    later, so every label here says which half hour or which day it means.
    """
    from statsbadge_octopus import USE_FIELDS

    labels = {name: entry["label"] for name, entry in USE_FIELDS.items()}
    for name, label in labels.items():
        assert "last" not in label.lower(), f"{name} is called {label!r}"
    assert labels["kwh"] == "Newest half hour", labels["kwh"]
    # The reading that says how stale the rest of them are.
    assert "behind" in labels["behind_h"].lower(), labels["behind_h"]

    # A graph of it ends where the readings end, which is what age_ms carries.
    source = fetched()
    ring = source.series()[f"{ELEC}.kwh"]
    assert ring["age_ms"] > 12 * 3600 * 1000, ring["age_ms"]
    frame = {}
    source.sample(frame, 1.0)
    assert abs(frame[ELEC]["behind_h"] - ring["age_ms"] / 3600000.0) < 0.6, (
        frame[ELEC]["behind_h"], ring["age_ms"])


@check
def test_a_meter_s_ring_ends_where_its_readings_do():
    """A meter reports a day late.

    A ring ending at now would be forty empty slots with the trace squeezed into the left
    of the plot, so it ends at the newest half-hour and says how far back that is.
    """
    source = fetched()
    rings = source.series()
    for ref in (f"{ELEC}.kwh", f"{GAS}.kwh"):
        entry = rings[ref]
        assert len(entry["points"]) == HISTORY_SLOTS
        assert all(point is not None for point in entry["points"]), ref
        assert entry["every_ms"] == 30 * 60 * 1000
        behind_h = entry["age_ms"] / 3600000.0
        assert 12 < behind_h < 36, f"{ref} is {behind_h:.1f}h behind"

    # The prices are current, so that ring ends at the slot in progress.
    price = rings[f"{ELEC}.price"]
    assert all(point is not None for point in price["points"])
    assert price["age_ms"] < 30 * 60 * 1000, price["age_ms"]


@check
def test_a_gap_in_the_readings_is_a_gap_in_the_ring():
    """Packing what arrived would draw the hours either side of a gap as adjacent."""
    source = Faked()
    source._refresh_account()
    newest = _slot_start(NOW)
    by_slot = {newest - datetime.timedelta(minutes=30 * step): 1.0
               for step in range(HISTORY_SLOTS) if step != 5}
    source._push_ring(ELEC, "kwh", by_slot, newest, NOW, 3)
    points = source._rings[ELEC]["kwh"]["points"]
    assert len(points) == HISTORY_SLOTS
    assert points[HISTORY_SLOTS - 1 - 5] is None, points
    assert sum(1 for point in points if point is None) == 1


@check
def test_nothing_is_asked_of_the_api_until_it_is_configured():
    """An extension nobody has given a key is unconfigured, and not broken."""
    quiet = Faked({})
    quiet.poll()
    assert quiet.asked == [], quiet.asked
    assert quiet.faults == 0, "an unconfigured source reported a fault"
    assert quiet.last_fault and "API key" in quiet.last_fault
    # The message goes as soon as a key is given, without waiting for the first reply.
    quiet.configure({"api_key": "sk_live_fake", "account_number": "A-1"})
    assert quiet.last_fault is None


@check
def test_nothing_is_asked_for_faster_than_it_changes():
    """Octopus documents no rate limit, which is a reason to be careful with it.

    Every clock is set by how fast the thing behind it moves. A standing charge held for
    months was being re-read every fifteen minutes, eight times an hour per tariff.
    """
    import statsbadge_octopus as octopus

    # A tariff publishes daily and a meter reports daily, so an hour covers both. A standing
    # charge and a tariff switch are slower still.
    assert octopus.EVERY >= 3600.0, octopus.EVERY
    assert octopus.USE_EVERY >= 3600.0, octopus.USE_EVERY
    assert octopus.STANDING_EVERY >= 12 * 3600.0, octopus.STANDING_EVERY
    assert octopus.ACCOUNT_EVERY >= 6 * 3600.0, octopus.ACCOUNT_EVERY
    # The slow ones are slower than the prices, which is the whole point of splitting them.
    assert octopus.STANDING_EVERY > octopus.EVERY
    assert octopus.ACCOUNT_EVERY > octopus.EVERY

    # An hour of it, counted per endpoint, for one electricity and one gas meter.
    source = Faked()
    source._refresh_account()
    source.asked.clear()
    for _ in range(int(3600 // octopus.EVERY)):
        source._refresh_rates()
    for _ in range(int(3600 // octopus.USE_EVERY)):
        source._refresh_use()
    standing = int(3600 // octopus.STANDING_EVERY)
    for _ in range(standing):
        source._refresh_standing()
    assert standing == 0, "a standing charge is being read within the hour"
    assert len(source.asked) <= 6, source.asked

    # A rejected key backs off rather than retrying flat for as long as nobody notices.
    waits = []
    for missed in range(6):
        source._missed = missed
        waits.append(source._wait())
    assert waits == sorted(waits) and waits[0] == octopus.RETRY_AFTER, waits
    assert waits[-1] <= octopus.RETRY_CEILING, waits
    assert waits[-1] > waits[0] * 4, waits


@check
def test_only_what_it_asked_for():
    """One request an hour for the account, one a tariff for the rates, one a meter."""
    source = fetched()
    assert source.asked.count("/accounts/A-1234ABCD/") == 1
    rates = [path for path in source.asked if "standard-unit-rates" in path]
    assert len(rates) == 2, rates
    # Three: the electricity point lists two meters and the first has nothing.
    used = [path for path in source.asked if path.endswith("/consumption/")]
    assert len(used) == 3, used
    assert source.last_fault is None, source.last_fault


def main():
    failed = []
    for fn in CHECKS:
        try:
            fn()
            print(f"ok   {fn.__name__}")
        except AssertionError as exc:
            failed.append((fn.__name__, exc))
            print(f"FAIL {fn.__name__}: {exc}")
        except Exception as exc:  # noqa: BLE001
            failed.append((fn.__name__, exc))
            print(f"ERR  {fn.__name__}: {type(exc).__name__}: {exc}")
    print(f"\n{len(CHECKS)} checks, {len(failed)} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
