# Bundled gazetteer provenance — `geonames_cities15000.tsv.gz`

The OSS free-text geocoder (`cograph_client/spatiotemporal/geocoder.py`,
`GeoNamesGeocoder`) resolves place names against this bundled file. It is a
**general, public** gazetteer — NOT a curated or persona-specific list. Any place
in it resolves because it is a real city in the public dataset.

## Source

- **Dataset:** GeoNames `cities15000` — every populated place on Earth with a
  population of **≥ 15,000**.
- **Download URL:** <https://download.geonames.org/export/dump/cities15000.zip>
- **Publisher:** GeoNames (<https://www.geonames.org/>)
- **License:** Creative Commons Attribution 4.0 International (**CC BY 4.0**) —
  <https://creativecommons.org/licenses/by/4.0/>. Attribution: "Data © GeoNames,
  licensed under CC BY 4.0."
- **Coverage:** ~34,000 cities across all 195+ countries and every US state.
- **Downloaded:** 2026-07-08.

## Bundled form (how this file was produced)

The upstream `cities15000.txt` is the standard GeoNames "geoname" TSV (19
columns). To keep the bundle small we retain only the 7 columns the geocoder
uses, then gzip:

```
# from the unzipped cities15000.txt (tab-separated):
#   col 2  = name          col 3  = asciiname     col 11 = admin1 code (US: state)
#   col 9  = country code   col 5  = latitude      col 6  = longitude
#   col 15 = population
awk -F'\t' 'BEGIN{OFS="\t"} {print $2,$3,$11,$9,$5,$6,$15}' cities15000.txt \
  | gzip -9 > geonames_cities15000.tsv.gz
```

Resulting bundled schema (tab-separated, one city per row):

| # | column       | example        |
|---|--------------|----------------|
| 1 | name         | `Irvine`       |
| 2 | asciiname    | `Irvine`       |
| 3 | admin1 code  | `CA` (US state)|
| 4 | country code | `US` (alpha-2) |
| 5 | latitude     | `33.66946`     |
| 6 | longitude    | `-117.82311`   |
| 7 | population   | `256927`       |

- Rows: ~34,000. On-disk: ~0.7 MB gzipped.
- No transformation of values beyond column selection — coordinates and
  populations are verbatim from GeoNames.

## Refreshing

Re-download `cities15000.zip`, unzip, and re-run the `awk | gzip` command above,
overwriting `geonames_cities15000.tsv.gz`. No code change is needed — the loader
in `geocoder.py` reads the columns above by position.
