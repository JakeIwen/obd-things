# ODX/PDX and diagnostic-source research — 2026-07-19

## Outcome

No exact 2022 ProMaster ODX/PDX package was found in the local service corpus or in the
publicly indexed sources searched on 2026-07-19. This is a search result, not proof that no
package exists. The useful free path is therefore to combine live ECU identity, authorized
AlfaOBD traces, OEM service documents, FCA calibration lists, and NHTSA filings while keeping
each inferred DID/routine definition per ECU.

Exact authenticated GitHub code searches on the same date returned no public hits for the live
identifiers `68524831AF`, `68532161AF`, `68516215AE`, `68517084AD`, `68516285AC`,
`68510377AC`, `TBM200A11P`, or `BC637M.0001`; a related-platform `BC250I.010` search also had
no usable database hit. General web searches for those identifiers plus ODX/PDX found
part/calibration metadata but no diagnostic-description package. OpenPilot/OpenDBC likewise had
no ProMaster definition. Record this as a dated negative search only; repository indexing and
public availability can change.

A second exact pass for radar identity `MRR1evo14F` found public Alfa Romeo/Fiat diagnostic
reports and a related Ducato replacement-part listing, but no ODX/PDX/CAN database; authenticated
GitHub code searches for that identity and the additional live part numbers also returned no code.
Those reports corroborate hardware-family reuse only and are not a source for this van's DIDs or
scaling.

A 2026-07-21 follow-up after the five-module `F100-F1FF` live inventory searched the new exact
supplier/configuration strings `13GJ6D0YN7CB`, `04446561007`, `TF0711552047510`, `AGSM637FCA`, and
`P7FK46LXHAD`, as well as the earlier `TBM200A11P`. Neither public indexes nor the local OEM corpus
produced an ODX/PDX or diagnostic-description hit. The official Mopar catalog does identify
[`68524831AF` as a Body Controller](https://store.mopar.com/oem-parts/mopar-body-controller-module-68524831af),
which corroborates the live BCM identity but adds no DID labels or encodings. Preserve this as a
dated negative search; the exact live values and raw-report provenance are in
[`2026-07-21_candidate_did_inventory.md`](2026-07-21_candidate_did_inventory.md).

The same 2026-07-21 pass searched the six successful BCM IO-control request/response identifiers
(`2F/6F 5040`, `5041`, `5050`, `5115`, `5118`, and `5120`) in contiguous and spaced forms across
public web and authenticated GitHub code indexes, and searched the local OEM corpus for those values
and IO-control terminology. It found no usable label/schema match. A public FCA CAN-monitor repository
contained only older Alfa Romeo broadcast captures and parser code, not diagnostic descriptions.
At the time of that search, the local AlfaOBD logs contained the commands but no corresponding UI
action labels and no APK/application database had yet been copied. On 2026-07-21 the owner-authorized
tablet extraction recovered AlfaOBD 2.4.4.0's internal catalog. It confirms front/rear door-lock relay
actions in the exact BCM profile's menu, but its menu tables do not directly bind those labels to the
six captured IO-control DIDs. A fresh labeled AlfaOBD capture—not another blind public search—therefore
remains the next source for the exact action-to-payload mapping. See
[`2026-07-21_alfaobd_apk_catalog.md`](2026-07-21_alfaobd_apk_catalog.md).

The local `/home/pi/dev/ram_2022_GAS` corpus (about 1.7 GB / 5,925 HTML documents) contains
service procedures and wiring/topology material, but the search found no `.odx`, `.pdx`, `.cdf`,
`.cdd`, `.a2l`, or `.dbc` diagnostic database. The exact service-document tree on `m4mac` was not
accessible from `vanpi` during the original check. A 2026-07-21 recheck again found `m4mac`
responding on the LAN while SSH (22), SMB (445), and AFP (548) were closed. That is a temporal
access observation, not a claim that the Mac lacks the files.

## Sources that did produce useful identity metadata

- FCA's official [J2534 calibration report](https://kb.fcawitech.com/assets/J2534_FedWorldReport.pdf)
  maps flash/software lineages to model/year/module. It corroborates, for example,
  `68532161AF` as a 2022 VF 3.6L 948TE TCM calibration. It is not an ODX database and does not
  provide DID scaling or routine schemas.
- FCA's official [J2534 manual](https://kb.fcawitech.com/assets/KB1.4.pdf) says the wiTECH 2.0
  J2534 application automatically downloads applicable flash files and supports data reads, DTCs,
  routines, and system tests. It does not document an ODX/PDX export. A lawful application cache is
  still worth inventorying, but treat flash payloads and diagnostic-description data as separate
  artifacts rather than assuming the cache is an ODX source.
- NHTSA-hosted FCA filings and TSBs provide exact part/module relationships. The
  [Part 573 IPC filing](https://downloads.regulations.gov/NHTSA-2023-0046-0001/attachment_1.pdf)
  identifies `68517084AD` as a Marelli Instrument Panel Cluster.
- The current-van AlfaOBD debug trace remains the best vehicle-grounded oracle already on hand.
  The newly extracted AlfaOBD catalog adds vendor-derived raw field layouts/scaling and module
  search candidates, but its numeric language placeholders need another indirection resolved and
  it is not proof of installed hardware or current-van values. Correlate it with fresh
  `Gauges_Data.log` and Debug Data exports.
- The exact-vehicle local OEM HTML corpus establishes topology, component names, DTC procedures,
  and operating conditions. It usually does not expose raw DID names/scales.
- Multiecuscan's public
  [supported-vehicle database](https://www.multiecuscan.net/supportedvehicleslist.aspx) has an
  unusually close Ducato `290MCA` module lineup (Silatech shifter, ZF 9HP48, Bosch DASM radar,
  Marelli cluster, Aptiv BCM). It is valuable for supplier attribution and missing-module search
  order, but its capability flags do not expose addresses, requests, DIDs, payloads, or scaling.
- ScanDoc's public
  [Jumper BCM demo](https://scandoc.online/last/0/18/26/2?lng=EN) and a community
  [X290 parameter guide](https://www.fiatforum.com/guides/multiecuscan-and-alfaobd-pids-for-x290-ducato.891/)
  provide human-readable parameter/checklist vocabulary only. The former is an older Marelli BCM,
  not this van's Aptiv unit; the latter is for a diesel Marelli 9DF engine ECU, and its tool
  parameter numbers are not wire-level DIDs.

Official Stellantis integrated diagnostics and service information are subscription products.
The [Stellantis independent-operator instructions](https://stellantisiop.com/iop/app/landing/standard_access)
describe TechAuthority and wiTECH J2534 subscription requirements; there is no free official
ODX download identified there.

## Free local analysis stack

The isolated environment `tmp/venvs/obd-research` contains:

| package | installed version | use |
|---|---:|---|
| `odxtools` | 11.3.1 | inspect/search/decode an ODX/PDX package if one is obtained legally |
| `python-can` | 4.6.1 | SocketCAN and capture plumbing |
| `can-isotp` | 2.0.7 | ISO-TP transport |
| `udsoncan` | 1.26.1 | UDS client/codec reference |
| `cantools` | 42.0.3 | DBC parsing and signal analysis |
| `canmatrix` | 1.2 | CAN database conversion |
| `scapy` | 2.7.0 | packet/protocol experimentation |
| `ddgs` | 9.14.4 | free search client (provider availability varies) |
| `pypdf` / `pdfplumber` | 6.14.2 / 0.11.10 | local OEM/TSB extraction |

`odxtools` is MIT-licensed and can list, find, browse, and decode a supplied package; its
[project documentation](https://pypi.org/project/odxtools/) does not include Stellantis data.
Eclipse [OpenSOVD](https://projects.eclipse.org/projects/automotive.opensovd) is another free
diagnostic implementation and includes ODX-conversion work, but likewise is tooling rather than
an OEM dataset.

## Public FCA cross-platform discovery leads

The independently maintained [FCA/Stellantis UDS read guide](https://magikh0e.pl/pubCarHacking/uds-reads.html)
documents community observations from Jeep-platform BCM/HVAC sweeps. Its 11-bit module addresses
and 125-kbit/s CAN-IHS examples are **not** the 2022 ProMaster endpoints live-verified here, so they
must not enter `lib/modules.py`. Its coarse populated-range observations are still useful for
ordering bounded searches after the standardized identity pass:

- `0000-00FF` — system/session values
- `2000-2FFF` — calibration/sensor-offset candidates
- `4000-4FFF` — diagnostic/snapshot candidates
- `A000-AFFF` — statistical/lifetime candidates
- `D000-DFFF` — IO-control-related namespaces on some FCA BCMs
- `F100-F1FF` — standardized plus OEM identity records

Treat these only as search-order priors. Start with a 256-DID page, compare NRC/response behavior
between modules, and expand a range only when it produces positives. Do not infer labels or scaling
from the Jeep examples. The same source also notes two reasons not to run naive 65,536-DID sweeps:
bus/wake load and rare OEM behaviors where a nominal read has a diagnostic side effect. The local
scanner therefore remains rate-limited, parked-gated, dry-run-first, checkpointed, and explicitly
gates expanded scans.

Example inspection once a package is available:

```bash
tmp/venvs/obd-research/bin/python -m odxtools list /path/to/package.pdx --services
tmp/venvs/obd-research/bin/python -m odxtools decode /path/to/package.pdx -D -d '22 F1 87'
```

## Best next source acquisitions

1. On `m4mac`, enable either Remote Login (SSH) or File Sharing long enough to expose
   `/Users/jacobr/Jake/J2534/service_docs/ram_2022_GAS_3`, then copy/search it read-only from
   `vanpi`. No desktop/browser session is required if SSH is enabled.
2. Export fresh AlfaOBD debug plus labeled gauges logs one module at a time. Record the exact
   selected profile, ignition/session state, and operator action so labels can be correlated
   without guessing.
3. Search any lawfully obtained wiTECH/J2534 cache or flash package by the live identities in
   the live-discovery finding. Treat calibration payloads and diagnostic description data as
   distinct artifacts.
4. Continue standardized identity reads (`F187`, `F188`, `F191`, `F192`, `F194`, `F132`, and
   ODX identifiers `F19E/F19F`) before broad DID scanning. On the five newly inventoried modules,
   `F19E`, `F19F`, and `F197` were unsupported in the inherited default sessions.
