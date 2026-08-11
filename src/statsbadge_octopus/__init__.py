"""Octopus Energy's half-hourly prices, and what the meters recorded, as readings.

Everything comes from `api.octopus.energy/v1` under one API key, in three kinds of request:

    /accounts/{number}      the meter points, their serials, and the tariff each is on.
                            The tariff code comes from here, and from nowhere else: no
                            other endpoint reports which one an account holds.
    /products/.../standard-unit-rates/
                            the prices, half an hour at a time. A public endpoint, and the
                            one request that reaches into tomorrow.
    /electricity-meter-points/.../consumption/
                            what the meter recorded, also half-hourly.

An Agile tariff is published a day ahead, so the interesting half of the curve has not
happened yet. `series()` cannot carry it: the collector clamps a ring's age to zero, and a
plot draws its newest point as now. So the slots ahead travel as a `list` field with
`forward_p_names` beside it, which is a `bars` page with the times down the side, and the
slots behind travel as a history ring for a graph.

`sample` works the current slot out of the curve it already holds, so the price on the
badge turns over on the half hour whatever the fetch interval is.

One meter point is one group, priced against the tariff that point is on. A house with gas
holds two, on two products, and a day costs what that meter's half-hours cost at the rates
which applied to them.
"""

import base64
import datetime
import json
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

from statsbadge.sources.base import Source

API = "https://api.octopus.energy/v1"

# A tariff publishes tomorrow's in one go each afternoon, and `sample` reads the current
# slot out of what is held, so this only has to have tomorrow before midnight.
EVERY = 900.0
# A switch is rare and the meter serials stay put for the life of the meter.
ACCOUNT_EVERY = 3600.0
# The meters report to Octopus a day or more behind, so this is asking whether yesterday
# has landed yet.
USE_EVERY = 1800.0
# What a failure waits before trying again. Shorter than the interval, and not forever.
RETRY_AFTER = 120.0
FETCH_POLL = 1.0

# Half an hour, which is the grid's settlement period and so the width of everything here.
SLOT_MS = 30 * 60 * 1000
SLOT_S = 30 * 60
SLOTS_A_DAY = 48

# How far either side of now the prices are asked for. Behind feeds the ring and the cost;
# ahead is what has been published, about 32 hours on Agile.
RATES_BACK_H = 48
RATES_AHEAD_H = 48
# One request covers all of that: 192 half-hours against a limit of 1500.
RATES_PAGE = 1500

# Six hours, which is the horizon for deciding when to put the washing on.
FORWARD_SLOTS = 12
HISTORY_SLOTS = SLOTS_A_DAY

# What a meter's consumption is asked for. A meter reporting 43 hours late has no complete
# local day inside two days, so this reaches back far enough to hold one anyway.
USE_BACK_H = 24 * 5
USE_PAGE = 1500

# What this says before it has a key and an account. Shown as a note and left out of the
# fault count: a silent source looks the same as an absent one.
UNSET = "no API key and account number set"

# Where the meter points are kept between runs, so the checkboxes are there before the first
# fetch lands and a save made while the network is down keeps what was ticked.
POINTS = "points"

# Gas arrives in kWh from a SMETS1 meter and in cubic metres from a SMETS2, and the API
# does not say which. The factor is 1.02264 volume correction by 39.5 MJ/m3 over 3.6.
GAS_M3_TO_KWH = 11.1868
GAS_UNITS = ("m3", "kWh")

# Prices are pence a kWh including VAT, as the bill charges them. Agile goes negative
# when the grid is oversupplied, so `peak` and a full scale are both left off: each reads a
# negative as an empty gauge, and a dial cannot draw one at all.
PRICE_FIELDS = {
    "price": {"label": "Price now", "unit": "p"},
    "standing_charge": {"label": "Standing charge", "unit": "p/day"},
    # Which product the readings are priced against. A page with no curve on it is a
    # tariff with no curve in it, and this is the only place that shows.
    "tariff": {"label": "Tariff"},
}

# The rest of the curve, for a tariff that has one. A fixed tariff gets none of these: every
# slot would hold the same number, so twelve identical bars say less than one reading and a
# graph of it is a straight line.
AGILE_FIELDS = {
    "price": {"label": "Price now", "unit": "p", "history": True},
    "price_next": {"label": "Price next slot", "unit": "p"},
    "price_min": {"label": "Cheapest published", "unit": "p"},
    "price_max": {"label": "Dearest published", "unit": "p"},
    "cheapest_in_h": {"label": "Cheapest slot in", "unit": "h"},
    "published_h": {"label": "Prices published for", "unit": "h"},
    # A lane per slot for a `bars` page, named by `forward_p_names` so the times read down
    # the side and not 0 upwards.
    "forward_p": {"label": "Next slots", "unit": "p", "list": True},
}

# What a meter recorded, which is never now: Octopus has the half-hours a meter has sent it,
# and a meter sends them a day or two later. Every label here says which half hour or which
# day it means, since "last half hour" beside a meter 19 hours behind is a promise of
# recency that none of these can keep.
USE_FIELDS = {
    "kwh": {"label": "Newest half hour", "unit": "kWh", "history": True},
    "kwh_day": {"label": "Newest full day", "unit": "kWh"},
    "cost_day": {"label": "Cost of that day", "unit": "p"},
    "behind_h": {"label": "Meter behind by", "unit": "h"},
}

# A companion travels beside the field it labels and is declared nowhere, so the field
# pickers never offer it.
LANE_NAMES = "forward_p_names"

# Whether a tariff has a curve is a matter of how wide its rows are, and not of how many
# arrived. Agile publishes only to 23:00 tomorrow, so counting what is ahead would take
# the fields off the page every evening.


class Octopus(Source):
    name = "octopus"
    label = "Octopus Energy"

    settings = (
        {"key": "api_key", "label": "API key", "type": "text", "secret": True,
         "hint": "From octopus.energy/dashboard/new/accounts/personal-details/api-access. "
                 "Starts with sk_live_."},
        {"key": "account_number", "label": "Account number", "type": "text",
         "hint": "On the same page, starting with an A."},
        {"key": "gas_units", "label": "What the gas meter reports", "type": "choice",
         "options": GAS_UNITS, "default": "m3",
         "hint": "A SMETS2 meter reports cubic metres and a SMETS1 reports kWh, and the "
                 "API does not say which. Out by a factor of eleven if this is wrong."},
    )

    every = EVERY

    @classmethod
    def available(cls):
        return True

    def __init__(self, config):
        super().__init__(config)
        # The meter points found on the account, and what was last read for each, keyed by
        # group. Both are replaced on the fetcher's thread and read while sampling, so both
        # go through the lock.
        self._points = []
        self._readings = {}
        # Every price each meter's tariff covers, as (start, end, pence) ascending, keyed by
        # group. Held whole because `sample` reads the current slot out of it and `_cost`
        # prices a meter's half-hours against it.
        self._rates = {}
        self._standing = {}
        # A ring per group, keyed by field.
        self._rings = {}
        self._lock = threading.Lock()
        self._next = 0.0
        self._next_account = 0.0
        self._next_use = 0.0
        self._fetcher = None
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._read_settings()

    # -- lifecycle ----------------------------------------------------------

    def start(self):
        """Take up the meter points the last run found, then fetch on a separate thread.

        Nothing in `sample` may wait on a network: every source shares the collector's
        thread and the first sample is taken while the server is still starting up.
        """
        with self._lock:
            self._points = [dict(point) for point in (self.store.get(POINTS) or ())]
        self._read_settings()
        if self._fetcher is None:
            self._stop.clear()
            self._fetcher = threading.Thread(target=self._fetch_loop, daemon=True,
                                             name="statsbadge-octopus")
            self._fetcher.start()

    def stop(self):
        self._stop.set()
        self._wake.set()
        if self._fetcher is not None:
            self._fetcher.join(timeout=2.0)
            self._fetcher = None

    def configure(self, settings):
        """Take settings while running, and ask again without waiting out the interval.

        A key pasted into the browser should turn up as a list of meters, and a meter ticked
        should turn up as a group, without anybody restarting the server.
        """
        super().configure(settings)
        self._read_settings()
        if self.last_fault == UNSET and self.key and self.account:
            # That message was about the settings, and they have just been given. Waiting
            # for a fetch to succeed first leaves the config page saying they are missing
            # for as long as the first request takes.
            self.last_fault = None
        self._next = self._next_account = self._next_use = 0.0
        self._wake.set()

    # -- what this source offers --------------------------------------------

    def _read_settings(self):
        """Re-read the settings, and rebuild what is offered from the meters now known.

        `settings` and `groups` are read off the source and not off the class, so the
        checkbox for a meter and the group it fills both appear as soon as the account has
        been asked what it holds.
        """
        self.key = str(self.config.get("api_key") or "").strip()
        self.account = str(self.config.get("account_number") or "").strip()
        self.gas_scale = (GAS_M3_TO_KWH
                          if str(self.config.get("gas_units") or "m3") == "m3" else 1.0)

        with self._lock:
            points = list(self._points)
            curved = {group for group, rates in self._rates.items() if _curved(rates)}
        self._watched = [point for point in points
                         if self.config.get(f"meter_{point['slug']}", True)]

        self.settings = tuple(Octopus.settings) + tuple(
            {"key": f"meter_{point['slug']}", "label": point["label"], "type": "bool",
             "default": True}
            for point in points)

        # Slow, all of it: the prices turn over on the half hour and a meter reports once a
        # day, against a badge that polls once a second.
        groups = {}
        for point in self._watched:
            fields = {**PRICE_FIELDS, **USE_FIELDS}
            if point["group"] in curved:
                fields.update(AGILE_FIELDS)
            groups[point["group"]] = {"label": point["label"], "slow": True,
                                      "fields": fields}
        self.groups = groups
        self.provides = tuple(groups)

    # -- sampling -----------------------------------------------------------

    def sample(self, frame, dt):
        """What the fetcher brought back, with the current slot worked out here.

        The price comes out of the curve already held, so it turns over on the half hour
        however long ago the last fetch was. Every value here is already in hand.
        """
        with self._lock:
            readings = {group: dict(values) for group, values in self._readings.items()
                        # A meter unticked a moment ago is still in the last answer, and a
                        # group nothing declares is a group nothing can draw.
                        if group in self.groups}
            rates = {group: list(found) for group, found in self._rates.items()}
            standing = dict(self._standing)
            tariffs = {point["group"]: point["product"] for point in self._watched}

        now = datetime.datetime.now(datetime.timezone.utc)
        for group in self.groups:
            values = readings.get(group) or {}
            values.update(_priced(rates.get(group) or (), now))
            if standing.get(group) is not None:
                values["standing_charge"] = standing[group]
            if tariffs.get(group):
                values["tariff"] = tariffs[group]
            declared = (self.groups[group].get("fields") or {})
            kept = {name: value for name, value in values.items()
                    if value is not None and (name in declared or name == LANE_NAMES)}
            if kept:
                frame[group] = kept

    def series(self):
        """The slots behind, per watched meter: prices half-hourly, and what was used.

        The collector would otherwise sample these at its interval, and ninety samples of a
        price that moves twice an hour is a minute and a half of staircase. These are on the
        settlement period, and say so: a plot walked by the host's sample count would slide
        a day an hour.
        """
        with self._lock:
            rings = {group: dict(fields) for group, fields in self._rings.items()
                     if group in self.groups}
        out = {}
        for group, fields in rings.items():
            declared = (self.groups[group].get("fields") or {})
            for field, entry in fields.items():
                if field in declared:
                    out[f"{group}.{field}"] = {"points": entry["points"],
                                               "every_ms": SLOT_MS,
                                               "age_ms": entry["age_ms"]}
        return out

    def note_fault(self, exc):
        """What Octopus said, without a type name in front of it.

        `readable` names the type of anything it does not recognise, which is right for a
        fault nobody expected and wrong for a message written to be read.
        """
        if isinstance(exc, OctopusError):
            self.faults += 1
            self.last_fault = str(exc)
            return
        super().note_fault(exc)

    # -- fetching -----------------------------------------------------------

    def _fetch_loop(self):
        while not self._stop.is_set():
            try:
                self._refresh()
            except Exception as exc:
                # The fetcher must not die, or the readings would stand at whatever they
                # last were with nothing ever replacing them.
                self.note_fault(exc)
            self._wake.wait(FETCH_POLL)
            self._wake.clear()

    def _refresh(self):
        if not self.key or not self.account:
            self.last_fault = UNSET
            return
        now = time.monotonic()
        if now >= self._next_account:
            try:
                self._refresh_account()
            except Exception as exc:
                self._next_account = now + RETRY_AFTER
                self.note_fault(exc)
                return
            self._next_account = time.monotonic() + ACCOUNT_EVERY

        worked = False
        if now >= self._next:
            try:
                self._refresh_rates()
            except Exception as exc:
                self._next = time.monotonic() + RETRY_AFTER
                self.note_fault(exc)
                return
            self._next = time.monotonic() + self.every
            worked = True

        # After the rates, so a day is priced against the tariff that applied to it rather
        # than against whatever the last run happened to hold.
        if now >= self._next_use:
            try:
                answered = self._refresh_use()
            except Exception as exc:
                self._next_use = time.monotonic() + RETRY_AFTER
                self.note_fault(exc)
                return
            self._next_use = time.monotonic() + USE_EVERY
            worked = worked or answered
            if not answered:
                # A meter reported nothing and said so. Clearing that here would leave the
                # line on screen for one poll in every thirty minutes.
                return

        if worked:
            self.note_ok()

    def _refresh_account(self):
        """The meter points on the account, with the tariff each is on.

        Export points are left out: a page of what a panel sold reads the same as a page of
        what the house bought, and the numbers mean the opposite.
        """
        body = self._get(f"/accounts/{urllib.parse.quote(self.account)}/")
        points = []
        for prop in body.get("properties") or ():
            for fuel, key in (("electricity", "electricity_meter_points"),
                              ("gas", "gas_meter_points")):
                for point in prop.get(key) or ():
                    found = _point_of(fuel, point)
                    if found is not None:
                        points.append(found)
        if not points:
            raise OctopusError(f"account {self.account} has no meter points to read")

        with self._lock:
            known = self._points
            self._points = points
        if points != known:
            self.store.set(POINTS, points)
            # A meter on the account is a checkbox and a group that were not there a moment
            # ago, so what this source offers is rebuilt here and not left until somebody
            # saves the settings again.
            self._read_settings()

    def _refresh_rates(self):
        """Every price either side of now, per watched meter that has a tariff.

        Behind feeds the ring and the cost; ahead is however much has been published. One
        request a tariff, and most accounts have one or two.
        """
        now = datetime.datetime.now(datetime.timezone.utc)
        slot = _slot_start(now)
        start = slot - datetime.timedelta(hours=RATES_BACK_H)
        end = slot + datetime.timedelta(hours=RATES_AHEAD_H)

        for point in list(self._watched):
            if not point["product"] or not point["tariff"]:
                continue
            rates = self._unit_rates(point, start, end)
            standing = self._standing_charge(point)
            with self._lock:
                self._rates[point["group"]] = rates
                self._standing[point["group"]] = standing
            if _curved(rates):
                # A flat tariff gets no ring: every point would be the same number, and one
                # row covering a year fills none of the slots anyway.
                self._push_ring(point["group"], "price",
                                {when: price for when, _until, price in rates},
                                slot, now, 2)
        # A tariff with a curve offers fields a fixed one does not, and whether it has one
        # is only known once its prices are in.
        self._read_settings()

    def _unit_rates(self, point, start, end):
        """A tariff's prices as (start, end, pence) ascending.

        The endpoint answers newest first and takes no `order_by`, so this sorts. Pence
        including VAT, as the bill charges them and as a price is quoted.
        """
        body = self._get(f"{self._tariff_path(point)}standard-unit-rates/"
                         f"?period_from={_stamp(start)}&period_to={_stamp(end)}"
                         f"&page_size={RATES_PAGE}")
        found = []
        for row in body.get("results") or ():
            when = _parse(row.get("valid_from"))
            price = row.get("value_inc_vat")
            if when is None or price is None:
                continue
            # A fixed tariff leaves valid_to null, meaning until further notice. Every
            # reader here asks which slot covers a moment, so an open end is a long one.
            until = _parse(row.get("valid_to")) or (when + datetime.timedelta(days=3650))
            found.append((when, until, float(price)))
        found.sort(key=lambda entry: entry[0])
        return found

    def _standing_charge(self, point):
        """Pence a day, whatever is in force now. None if the tariff does not say."""
        try:
            body = self._get(f"{self._tariff_path(point)}standing-charges/?page_size=10")
        except OctopusError:
            # A tariff without one is ordinary: the unit price is the reading that matters,
            # and an export tariff carries no standing charge.
            return None
        now = datetime.datetime.now(datetime.timezone.utc)
        for row in body.get("results") or ():
            when = _parse(row.get("valid_from"))
            until = _parse(row.get("valid_to"))
            if when is not None and when <= now and (until is None or now < until):
                value = row.get("value_inc_vat")
                return None if value is None else round(float(value), 2)
        return None

    def _tariff_path(self, point):
        return (f"/products/{urllib.parse.quote(point['product'])}"
                f"/{point['fuel']}-tariffs/{urllib.parse.quote(point['tariff'])}/")

    def _refresh_use(self):
        """What each watched meter recorded, and what the last full day of it cost.

        True when every meter answered. A meter that reported nothing is one meter and not
        the account, so the others are still read - but note_ok() would clear the line
        saying so, and the caller needs to know not to.
        """
        every = True
        for point in list(self._watched):
            with self._lock:
                rates = list(self._rates.get(point["group"]) or ())
            try:
                values, used = self._use_of(point, rates)
            except OctopusError as exc:
                self.note_fault(exc)
                every = False
                continue
            with self._lock:
                self._readings.setdefault(point["group"], {}).update(values)
            if used:
                # Ending at the meter's newest half-hour and not at now. A meter reports a
                # day late, so a ring ending now would be forty empty slots and a trace
                # squeezed into the left of the plot.
                self._push_ring(point["group"], "kwh", dict(used), used[-1][0],
                                datetime.datetime.now(datetime.timezone.utc), 3)
        return every

    def _use_of(self, point, rates):
        """One meter's half-hours as readings, and the half-hours themselves for a ring.

        Each serial the point lists is asked in turn, since an exchanged meter answers with
        an empty list and the live one is not always first.
        """
        now = datetime.datetime.now(datetime.timezone.utc)
        start = _slot_start(now) - datetime.timedelta(hours=USE_BACK_H)
        scale = self.gas_scale if point["fuel"] == "gas" else 1.0
        serials = point.get("serials") or ([point["serial"]] if point.get("serial") else [])

        used = []
        for serial in serials:
            body = self._get(
                f"/{point['fuel']}-meter-points/{urllib.parse.quote(point['id'])}"
                f"/meters/{urllib.parse.quote(serial)}/consumption/"
                f"?period_from={_stamp(start)}&period_to={_stamp(now)}"
                f"&order_by=period&page_size={USE_PAGE}")
            for row in body.get("results") or ():
                when = _parse(row.get("interval_start"))
                amount = row.get("consumption")
                if when is not None and amount is not None:
                    used.append((_slot_start(when), float(amount) * scale))
            if used:
                break
        if not used:
            # Said out loud, since the alternative is a group with a price on it and no
            # readings, which looks the same as an extension that is broken.
            raise OctopusError(
                f"{point['label']}: {', '.join(serials)} reported nothing in the last "
                f"{USE_BACK_H // 24} days")

        used.sort(key=lambda entry: entry[0])
        newest, latest = used[-1]
        day = _last_full_day(used)
        return {
            "kwh": round(latest, 3),
            "behind_h": round((now - newest).total_seconds() / 3600.0, 1),
            "kwh_day": None if day is None else round(sum(a for _w, a in day), 2),
            "cost_day": _cost(day, rates) if day else None,
        }, used

    def _push_ring(self, group, field, by_slot, newest, now, places):
        """A day of half-hours ending at `newest`, placed by their slots.

        Placed and not packed in the order they arrived: a gap would otherwise draw the
        hours either side of it as adjacent. A missing slot is None, which is how a plot
        draws a gap where there was no reading.

        `age_ms` is how far back `newest` is, so a plot can place a day-old
        trace at the right end of its axis.
        """
        oldest = newest - datetime.timedelta(minutes=30 * (HISTORY_SLOTS - 1))
        points = []
        for step in range(HISTORY_SLOTS):
            value = by_slot.get(oldest + datetime.timedelta(minutes=30 * step))
            points.append(None if value is None else round(value, places))
        with self._lock:
            self._rings.setdefault(group, {})[field] = {
                "points": points,
                "age_ms": int((now - newest).total_seconds() * 1000),
            }

    # -- talking to it ------------------------------------------------------

    def _get(self, path):
        """One GET against the API, with the key as the username of a basic auth header.

        Built here rather than through urllib's handler: that one offers credentials after a
        401, and these endpoints answer 403 without asking.
        """
        token = base64.b64encode(f"{self.key}:".encode()).decode("ascii")
        request = urllib.request.Request(API + path, headers={
            "Authorization": f"Basic {token}",
            "Accept": "application/json",
        })
        try:
            with urllib.request.urlopen(request, timeout=20) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            raise OctopusError(_said(exc, path)) from exc


class OctopusError(Exception):
    """What Octopus said was wrong, as one line for the config UI to show."""


def _said(exc, path):
    """An HTTP failure as something to act on.

    The status alone says little here: a wrong key and a wrong account number are both a
    403, and the body names which.
    """
    detail = ""
    try:
        body = json.loads(exc.read().decode("utf-8"))
        detail = str(body.get("detail") or body.get("error") or "")
    except Exception:  # noqa: BLE001
        detail = ""
    if exc.code in (401, 403):
        return detail or "the API key was refused"
    if exc.code == 404:
        return detail or f"nothing at {path.split('?')[0]}"
    return f"HTTP {exc.code} {exc.reason}" + (f": {detail}" if detail else "")


def _point_of(fuel, point):
    """One meter point as what this source needs, or None if it cannot be drawn.

    A point needs a meter to read and an identifier to name it. An export point is left
    out: its readings mean the opposite of an import point's, and both draw the same page.
    """
    if point.get("is_export"):
        return None
    # Every one of them, newest last as the account lists them. A meter point carries the
    # meters it has ever had, and an exchanged one answers with no readings at all, so the
    # first is a guess.
    serials = [str(meter.get("serial_number") or "")
               for meter in point.get("meters") or ()]
    serials = [serial for serial in serials if serial]
    identifier = str(point.get("mpan") or point.get("mprn") or "")
    if not serials or not identifier:
        return None

    tariff = _current_tariff(point.get("agreements") or ())
    # The flag says so where an account carries one; the tariff says so where it does not.
    if "OUTGOING" in tariff:
        return None
    return {
        "fuel": fuel,
        "id": identifier,
        "serials": serials,
        "tariff": tariff,
        "product": _product_of(tariff),
        "label": ("Electricity" if fuel == "electricity" else "Gas")
                 + f" {identifier[-4:]}",
        "slug": f"{fuel[:4]}_{identifier[-4:]}",
        "group": f"octopus_{fuel[:4]}_{identifier[-4:]}",
    }


def _current_tariff(agreements):
    """The tariff code in force now, or the most recent one otherwise.

    An account carries every agreement it has ever had, and a switch that starts tomorrow
    sits in there beside the one running today.
    """
    now = datetime.datetime.now(datetime.timezone.utc)
    live = ""
    latest_at, latest = None, ""
    for agreement in agreements:
        code = str(agreement.get("tariff_code") or "")
        if not code:
            continue
        start = _parse(agreement.get("valid_from"))
        until = _parse(agreement.get("valid_to"))
        if start is not None and start <= now and (until is None or now < until):
            live = code
        if start is not None and (latest_at is None or start > latest_at):
            latest_at, latest = start, code
    return live or latest


def _product_of(tariff):
    """`E-1R-AGILE-24-10-01-M` as `AGILE-24-10-01`.

    The rate endpoints are keyed by product and the account only names a tariff. A tariff
    code is the fuel and rate count, the product, then the region letter, so the product is
    what is left when both ends come off.
    """
    parts = (tariff or "").split("-")
    if len(parts) < 4:
        return ""
    return "-".join(parts[2:-1])


def _curved(rates):
    """Whether a tariff prices by the settlement period, or by one rate until further notice.

    Read off the width of the rows and not off how many arrived. Agile publishes only as far
    as 23:00 tomorrow, so counting what is ahead would drop the curve every evening.
    """
    return any((until - when).total_seconds() <= SLOT_S for when, until, _price in rates)


def _priced(rates, now):
    """The readings that are a matter of where `now` falls in the curve.

    Worked out here rather than fetched, so the badge turns over on the half hour. A tariff
    answering with one long slot is a fixed rate, and gets the price alone: the cheapest of
    twelve identical numbers is not worth a reading.
    """
    if not rates:
        return {}
    slot = _slot_start(now)
    ahead = [(when, price) for when, _until, price in rates if when >= slot]
    if not ahead:
        # Nothing published past the slot in progress, so the last price still standing is
        # the one being paid.
        holding = [(when, price) for when, until, price in rates if until > now]
        return {"price": round(holding[0][1], 2)} if holding else {}

    forward = ahead[:FORWARD_SLOTS]
    out = {"price": round(forward[0][1], 2)}
    if not _curved(rates):
        return out

    cheapest = min(forward, key=lambda entry: entry[1])
    out.update({
        "price_next": round(ahead[1][1], 2),
        "price_min": round(min(price for _w, price in ahead), 2),
        "price_max": round(max(price for _w, price in ahead), 2),
        "cheapest_in_h": round((cheapest[0] - slot).total_seconds() / 3600.0, 1),
        "published_h": round(len(ahead) * SLOT_S / 3600.0, 1),
        "forward_p": [round(price, 2) for _w, price in forward],
        # Local time, since the badge is read in the room the meter is in.
        LANE_NAMES: [when.astimezone().strftime("%H:%M") for when, _p in forward],
    })
    return out


def _slot_start(when):
    """The settlement period `when` falls in, which is the hour or the half hour."""
    return when.replace(minute=0 if when.minute < 30 else 30, second=0, microsecond=0)


def _last_full_day(used):
    """The most recent local day the meter reported all of, as its half-hours.

    Whole days only: a day with an hour missing reads as a quiet one, and its cost is short
    by the same amount. Local, because a bill is a day where the house is.
    """
    by_day = {}
    for when, amount in used:
        by_day.setdefault(when.astimezone().date(), []).append((when, amount))
    for day in sorted(by_day, reverse=True):
        if len(by_day[day]) >= SLOTS_A_DAY:
            return by_day[day]
    return None


def _cost(used, rates):
    """What a day of half-hours cost, in pence, priced slot by slot.

    Every slot has to have a price, or the total is short without saying so: a day priced at
    four fifths of itself reads as a cheap day, which is worse than no total at all.
    """
    if not used or not rates:
        return None
    prices = {}
    for when, until, price in rates:
        # A fixed rate is one row covering months, so it is spread over the slots it covers
        # and not looked up by its start.
        if (until - when).total_seconds() > SLOT_S:
            continue
        prices[when] = price
    flat = [price for when, until, price in rates
            if (until - when).total_seconds() > SLOT_S]

    total = 0.0
    for when, amount in used:
        price = prices.get(when)
        if price is None:
            price = flat[0] if len(flat) == 1 else None
        if price is None:
            return None
        total += amount * price
    return round(total, 1)


def _stamp(when):
    """A datetime as the API takes it: UTC, to the second, with a Z."""
    return when.astimezone(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse(text):
    """One of the API's datetimes, or None. Always offset-aware, so two can be compared."""
    if not text:
        return None
    try:
        when = datetime.datetime.fromisoformat(str(text).replace("Z", "+00:00"))
    except ValueError:
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=datetime.timezone.utc)
    return when
