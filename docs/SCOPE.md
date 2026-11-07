# What is outside the public release, and why

The audit reads third-party artefacts. Three classes of material are withheld from the public record.

## 1. Raw crawl snapshots

Eight fetch days across seven registries. The snapshots reproduce registry content in bulk, the registries' own terms do not license wholesale redistribution, and a snapshot ages into a misleading artefact once the registry changes.

## 2. Cloned repository contents

86,983 depth-1 clones at 86.4% success. The clones are third-party source code under their own licences. None of it was executed.

## 3. Per-unit records and owner identification

Per-unit files carry the normalised repository URL of each listing. Combined with a clone outcome or an adverse count they identify an account, and the paper states that it prints no owner label, account name, URL, adverse count or account-level clone outcome.

Five derived files that carry a repo or owner column are excluded from `data/derived_tables/`, namely `d_cases.csv`, `d_farm_owners.csv`, `d_farm_status.csv`, `d_reclone_cases.csv` and `d_sim.csv`. Their aggregate counterparts are included.

## Named-repository and named-owner material

`case_studies.py` selects the four repositories reported as case studies, and the working tables record the ten largest owners by account name. The paper prints none of these identifiers, so neither is released. The aggregate counts that the case-study section relied on are in `data/derived_tables/`.

## Route to the withheld material

Requests are routed through the journal editorial office, which can verify a requester and can have the named records checked. This is the route stated in the paper's Data availability statement.
