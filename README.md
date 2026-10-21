# MVA Hackathon 2026: Track 1 pipeline and Track 2 proposal (jag8)

Entry to *Rare Disease, Real Kid: The MVA Hackathon 2026* (Sage Bionetworks / Hugging Face).
Submissions are released under CC BY 4.0, per the hackathon rules.

## Contents

| Path | Purpose |
|---|---|
| `scripts/track1.py`, `scripts/track1.sh` | Track 1 pipeline: panel-gene extraction, normalization, local `bcftools csq` annotation, inheritance models, deterministic ranking, freeze, export |
| `config/disease-panels/mva.yml` | Literature-curated MVA gene panel (genes, inheritance, citations; no coordinates) |
| `config/track1.json` | Pipeline configuration (storage paths shown as placeholders) |
| `config/exomiser-unbiased.yml` | Exomiser job template read by the optional `unbiased` stage (not run for the submission) |
| `tests/test_track1_core.py` | Unit and `bcftools csq` integration tests on synthetic fixtures |
| `docs/jag8_track1_report.md` | Track 1 report |
| `docs/jag8_track2_report.pdf`, `docs/jag8_track2_report.md` | Track 2 submission report and source |
| `docs/figures/`, `docs/build_report.py` | Report figures and local PDF renderer |
| `docs/jag8_track1_methods.xlsx`, `docs/jag8_track2_methods.xlsx` | Methods descriptions (official template) |
| `SUBMISSION.md` | Materials checklist and links to the live contest requirements |

Track 2 is a literature and mechanism analysis with a proposed experimental design; no drug experiments were run. Its evidence is cited in the report. The renderer reproduces the document, rather than generating scientific results.

## Reproducing

The hackathon data are gated and are not in this repository, nor are the submitted prediction file or any patient-derived output.
With authorized access, place the VCF and its index at the `inputs` paths in `config/track1.json`, set the `storage` paths, and install `bcftools` 1.24 and Python 3.

```bash
python3 -m unittest discover -s tests          # synthetic fixtures only
./scripts/track1.sh setup-light                # Ensembl release 116 gene models
./scripts/track1.sh disease-informed --panel config/disease-panels/mva.yml
./scripts/track1.sh export-submission --run <run-id> --rows 1
```

The pipeline writes the run's sample identifier into `proband_id`. The official scorer accepts only `PROBAND01`, so that single field was replaced before upload.
The frozen run used the same panel (verified by SHA-256) and the same code, with local storage paths in the configuration.
A fresh local reproduction on 19 October 2026 matched the frozen ranked shortlist and submission hashes; the upload CSV matches after the required ID mapping. Patient-derived outputs remain private.
Reference: Broad/GATK GRCh38 (`Homo_sapiens_assembly38.fasta.gz`); annotation: Ensembl release 116.

## Rebuilding the Track 2 report

On macOS with the Georgia and Arial system fonts, install `reportlab`, then run:

```bash
python3 docs/build_report.py
```

The renderer uses only the included Markdown and figures and makes no network requests.

## AI assistance

Anthropic Claude Pro and OpenAI ChatGPT Plus (consumer plans), model-training opt-out enabled, no feedback provided. Ranking and filtering are deterministic code.

Pitch narration was generated with Google Gemini 3.8 Flash TTS.

## Dataset citation

Andreoletti G et al. *Rare Disease, Real Kid. The 2026 MVA Hackathon*. Synapse, 2026. [doi:10.7303/SYN76251147](https://doi.org/10.7303/SYN76251147). Controlled-access inputs are not distributed with this repository.

## Acknowledgement

This work was made possible through the Hackathon, organized by Sage Bionetworks in partnership with the MVA Society, Hugging Face, and BEACON (The Benchmarking, Evaluation, and Assessment Consortium for Science), with prize sponsorship from AWS and Anthropic. We are deeply grateful to the child and their family who generously contributed their data and their story to advance research into this rare disease. We acknowledge their trust in making this Hackathon possible.
