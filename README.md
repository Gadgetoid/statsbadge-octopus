# statsbadge-octopus

Your Octopus Energy prices and meter readings as readings, for [statsbadge](https://github.com/pimoroni/statsbadge).

On an Agile tariff the useful half of the price curve has not happened yet, so the next six hours travel as **bars with the times down the side**: put the washing on at the short one.

On any tariff, each meter reports what it used, what that day cost, and a half-hourly graph of it. A flat tariff has no curve to draw, so the bars and the cheapest-slot readings are not offered for one - the **Tariff** reading is there to say which you are on.

## Install

```bash
statsbadge ext add octopus
```

Then, in the config UI under **Extensions**, paste an API key and your account number. The meters appear as checkboxes on the next reload, and each one becomes a source in the field pickers.

## The key

Both are on [octopus.energy/dashboard/new/accounts/personal-details/api-access](https://octopus.energy/dashboard/new/accounts/personal-details/api-access): the key starts `sk_live_`, the account number with an `A`.

The key is stored in the host's config file in plain text. It is read-only and gives access to your account's meter readings, so treat it like a password.

## Settings

| Setting | What it does |
| ------- | ------------ |
| API key | The key above |
| Account number | Used once an hour, to find your meters and the tariff each is on |
| What the gas meter reports | `m3` or `kWh` - see below |
| One per meter | Whether to watch it |

Nothing else needs setting. The tariff, the region, the product code and the meter serials all come off the account, so switching tariff needs no edit here.

## What each meter reports

| Reading | What it is |
| ------- | ---------- |
| Price now | Pence a kWh including VAT, for the half hour in progress |
| Standing charge | Pence a day |
| Tariff | The product the readings are priced against, so a page with no curve says why |
| Last half hour | The most recent reading the meter has sent Octopus |
| Last full day | A whole local day, so a part-reported day is skipped rather than read as a quiet one |
| Cost that day | That day's half-hours, each priced at the rate that applied to it |
| Meter reported | How many hours behind the meter is. Two days is normal |

On a tariff with a half-hourly curve, also:

| Reading | What it is |
| ------- | ---------- |
| Next slots | A lane per half hour for the next six, for a **bars** page |
| Price next slot | The half hour after this one |
| Cheapest published / Dearest published | Across everything published ahead |
| Cheapest slot in | Hours until it, so a **grid** page can say "cheapest in 1.5h" |
| Prices published for | How far ahead the tariff has been published, which is how you tell tomorrow has landed |

## Pages worth building

| Page | Kind | Field |
| ---- | ---- | ----- |
| The next six hours | bars | `Next slots` |
| Today's shape | graph | `Price now` |
| At a glance | grid | `Price now`, `Cheapest slot in`, `Last full day`, `Cost that day` |
| Where it is going | trend | `Price now` |

## Gas

Octopus hands gas consumption over in kWh from a SMETS1 meter and in cubic metres from a SMETS2, and the API does not say which you have. The setting defaults to `m3`, which is the common case now.

If gas readings look about eleven times too small or too large, that setting is the wrong way round.

## Agile goes negative

When the grid is oversupplied the price drops below zero and you are paid to use electricity. Nothing here clamps that, so:

- a **grid**, **text** or **trend** page shows a negative fine
- a **dial** cannot draw one, and reads as empty
- a **bars** page draws a negative lane as nothing

So a dial is the wrong page for a price. Use it for `Last full day` if you want one.

## What it asks for, and how often

| Request | How often |
| ------- | --------- |
| `/accounts/{number}` | Hourly. Somebody switching tariff is not worth polling for |
| `/products/.../standard-unit-rates/` | Every fifteen minutes, per tariff |
| `/products/.../standing-charges/` | With the rates |
| `/{fuel}-meter-points/.../consumption/` | Every half hour, per meter, asking whether yesterday has landed |

The price on the badge turns over on the half hour whatever the fetch interval is: the whole published curve is held, and the slot covering now is read out of it on every sample.

Export meter points are skipped. What a panel sold and what the house bought would draw the same page and mean the opposite.

## Licence

MIT
