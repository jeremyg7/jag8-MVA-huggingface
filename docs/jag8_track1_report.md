# Track 1: From Five Million Genome Records to One Leading MVA Model

## At a Glance

| Item | Summary |
| --- | --- |
| **Question** | Which genomic variants best explain a supplied diagnosis of Mosaic Variegated Aneuploidy (MVA)? |
| **Search** | 5,012,204 genome-wide records → 14 relevant variants → 9 genetic models |
| **Top result** | `BUB1B`: stop-gained + missense |
| **Interpretation** | Possible compound heterozygosity; phase not established |
| **Robustness** | v2 code corrections and an alternative ranking sensitivity check left the result unchanged |
| **Method** | Disease-informed, local, deterministic, and reproducible |

MVA was already the supplied diagnosis. The task was therefore to identify the
causal variant or variants within a known disease context, not to infer a
diagnosis from scratch. A literature-informed workflow reduced the genome-wide
VCF to one uniquely top-ranked genetic model and froze that result before
challenge scoring.

## From Five Million Records to One Model

```text
5,012,204 genome-wide records
        ↓
1,285 records near MVA-related genes
        ↓
14 potentially gene-altering variants
        ↓
6 genes with qualifying variants
        ↘
9 plausible genetic models
        ↘
1 uniquely top-ranked model
```

“Potentially gene-altering” means coding or splice-relevant by sequence
annotation, not experimentally proven damaging.

| Stage | Unit | Count |
| --- | --- | ---: |
| Source VCF | records | 5,012,204 |
| Padded panel-gene regions | records | 1,285 |
| Normalized, split representation | biallelic alleles | 1,295 |
| Present as a non-reference sample call | biallelic alleles | 1,295 |
| Coding or splice-relevant | gene–allele observations | 14 |
| Passing primary quality criteria | gene–allele observations | 14 |
| Retained only through quality rescue | gene–allele observations | 0 |
| Genes with qualifying alleles | genes | 6 |
| Inheritance-compatible models | architectures | 9 |
| Ranked models | architectures | 9 |
| Frozen prediction | submission rows | 1 |

Splitting records with multiple alternate alleles explains the increase from
1,285 records to 1,295 biallelic alleles. Because the units then change from
records to alleles, genes, and genetic models, this is a search-space narrative
rather than a mathematically uniform funnel.

## Method

1. **Validate the input.** The immutable VCF and index were checked for GRCh38
   build clues, contig style, integrity, and a single sample. The source hash
   was recorded. All eight supplied HPO identifiers were extracted, validated,
   and preserved exactly; HPO semantic similarity did not contribute to the
   executed ranking.

2. **Define the disease knowledge layer.** Before candidate ranking, the MVA
   literature was used to define genes, aliases, disease tiers, and inheritance
   modes [6–16]. The panel covered canonical recessive MVA genes (`BUB1B`,
   `CEP57`, `TRIP13`, `CENATAC`), related `SLF2`/`SMC5` disorders, recessive
   MVA-like genes (`BUB1`, `MAD1L1`, `MAD2L1BP`, `CEP192`), and dominant
   `CDC20`. It contained no case-specific coordinates or target-gene override.

3. **Extract the relevant neighborhoods.** Ensembl release 116 gene models [4]
   resolved one interval per panel gene, padded by ±2 kb to retain nearby and
   splice-adjacent observations. Only intersecting VCF records were read.

4. **Normalize representation.** Targeted records were normalized against the
   matching Broad/GATK GRCh38 FASTA [5], split into biallelic records, and
   checked for preservation of genotype fields. This gave every downstream
   stage one consistent GRCh38 representation.

5. **Annotate consequences locally.** `bcftools csq` 1.24 [3], using the
   matching FASTA and panel-filtered Ensembl GFF3, assigned gene, coding, and
   splice consequences without sending gated variants to a remote service.
   Population allele frequency and ClinVar did not contribute to this local-CSQ
   baseline.

6. **Apply transparent quality criteria.** Variants were required to pass the
   VCF filter and, when available, meet DP ≥ 10 and GQ ≥ 20. Missing quality
   values were recorded as not evaluable rather than silently treated as
   failures; lower-quality observations were retained separately for review.
   All 14 relevant observations passed the primary path, and none depended on
   quality rescue.

7. **Construct and rank genetic models.** Qualifying alleles were assembled
   into recessive homozygous, possible compound-heterozygous, or
   dominant-compatible models according to the predefined inheritance layer.
   Ranking used disease tier, inheritance completeness, consequence evidence,
   quality status, available phase evidence, and stable tie-breakers. The
   filtering and ranking were deterministic; no trained or conversational
   model assigned the final rank.

8. **Apply the clarity gate and freeze.** A one-row prediction was permitted
   only when one primary coding/splice model was uniquely strongest, required
   no rescue allele, and had no equivalent same-tier competitor. The gate
   passed. Commands, structured outputs, hashes, and configuration/reference
   identifiers were frozen before any challenge score was viewed.

## Leading Genetic Model

Two heterozygous changes were identified in `BUB1B`, a recessive MVA gene: one
stop-gained change and one missense change. Together they are consistent with a
possible compound-heterozygous explanation; phase was not established [6,7].

A stop-gained change introduces a premature termination signal; a missense
change alters one encoded amino acid. These are predicted consequences, not
functional validation. Parental segregation or another suitable phasing method
would be required to establish whether the variants are in trans. The model is
therefore a strong genetic explanation for the known MVA phenotype, not a
clinically proven conclusion.

## Design Choices and Robustness

### Disease-informed by design

Using established MVA genetics was an intentional prior appropriate to the
question. The panel and inheritance assumptions were defined before ranking,
with no case-specific gene override or leaderboard-derived prior. Within the
canonical MVA tier, `BUB1B` received no special weighting. This was explicitly
a disease-informed analysis, not a genome-wide unbiased analysis.

> **Sensitivity check:** We repeated the ranking with inheritance and
> consequence evidence placed ahead of disease tier. All nine candidate models
> remained in the same order, and the `BUB1B` compound-heterozygous model
> remained uniquely first.

Thus, the leading result did not depend solely on disease-tier ordering.

### Independent v2 code audit

Independent review identified implementation issues in consequence
normalization, interval construction, and gene-alias handling. After correction
and rerun, the 14 relevant variants, six candidate genes, nine genetic models,
complete ranking order, leading `BUB1B` model, and final prediction were
unchanged.

The corrected per-gene intervals changed the obsolete v1 panel-region count of
42,647 to the v2 count of 1,285. Native `bcftools csq` splice vocabulary is now
normalized correctly and covered by a real-tool integration fixture. Gene
aliases are canonicalized before model construction. These corrections changed
the implementation and intermediate accounting, but not the biological result.

### AI assistance and escalation choices

Anthropic Claude Pro and OpenAI ChatGPT Plus (consumer plans; model-training
opt-out enabled; no feedback provided) supported literature synthesis,
implementation, testing, review, and documentation. Filtering, consequence
classification, inheritance-model construction, ranking, clarity gating, and
freezing remained deterministic program logic.

Exomiser, full VEP, population databases, and raw-read review were available as
escalation paths. They were deferred because the minimal workflow produced a
unique result that survived both code corrections and ranking sensitivity
analysis. Additional complexity would be introduced when it could answer a
specific unresolved question, not as an end in itself; these resources remain
important in broader clinical-genetics workflows.

## Limitations and Reproducibility

Population allele frequency and ClinVar status were not evaluated in the
executed baseline. Phase and familial segregation were not established, and
the molecular effects were predicted rather than experimentally validated.
The analysis also did not assess novel causal genes outside the panel,
deep-intronic causes, copy-number or structural variants, or raw-read evidence.

The architecture is reusable as a disease-informed framework, but each new
disorder requires an independently curated knowledge layer, appropriate
inheritance and consequence assumptions, and disease-specific validation.
Agreement with the hidden NHS answer would show benchmark agreement, not
independent clinical validation.

Reproducibility rests on pinned assumptions (`bcftools` 1.24, Broad/GATK
GRCh38, and Ensembl release 116), deterministic structured outputs, command and
checksum manifests, and an immutable frozen run. All 28 tests pass, including a
real-`bcftools csq` splice integration fixture. These tests validate software
behavior, not the biological truth of the candidate.

The public repository contains code, configuration, documentation, and
synthetic fixtures. It contains no gated genomic or phenotype inputs, patient
identifiers, exact private coordinates, patient-derived candidate tables,
frozen private runs, or submission outputs. Exact challenge reproduction
therefore requires authorized access to the gated dataset [1,2].

## References

1. Sage Bionetworks. [Rare Disease, Real Kid: The MVA Hackathon
   2026](https://sagebio-rare-disease-real-kid-mva-hackathon-2026.hf.space/).
2. Sage Bionetworks. [MVA Hackathon 2026 gated
   dataset](https://huggingface.co/datasets/SageBio/mva-hackathon-2026-data).
3. Danecek P, et al. [`bcftools csq` consequence-calling
   documentation](https://samtools.github.io/bcftools/howtos/csq-calling.html).
4. Ensembl. [Release 116](https://ftp.ensembl.org/pub/release-116/).
5. Broad Institute. [GATK GRCh38 assembly
   reference](https://github.com/broadinstitute/gatk/blob/master/src/test/resources/large/Homo_sapiens_assembly38.fasta.gz).
6. Malumbres M, Villarroya-Beltri C. [Mosaic variegated aneuploidy in
   development, ageing and cancer](https://doi.org/10.1038/s41576-024-00762-6).
   *Nature Reviews Genetics*. 2024;25:864–878.
7. Hanks S, et al. *Nature Genetics*. 2004. [PMID
   15475955](https://pubmed.ncbi.nlm.nih.gov/15475955/) (`BUB1B`).
8. Snape K, et al. *Nature Genetics*. 2011. [PMID
   21552266](https://pubmed.ncbi.nlm.nih.gov/21552266/) (`CEP57`).
9. Yost S, et al. *Nature Genetics*. 2017. [DOI
   10.1038/ng.3883](https://doi.org/10.1038/ng.3883) (`TRIP13`).
10. de Wolf B, et al. *The EMBO Journal*. 2021. [DOI
    10.15252/embj.2020106536](https://doi.org/10.15252/embj.2020106536)
    (`CENATAC`).
11. Grange LJ, et al. *Nature Communications*. 2022. [PMID
    36333305](https://pubmed.ncbi.nlm.nih.gov/36333305/) (`SLF2`, `SMC5`).
12. Carvalhal S, et al. *Science Advances*. 2022. [PMID
    35044816](https://pubmed.ncbi.nlm.nih.gov/35044816/) (`BUB1`).
13. Villarroya-Beltri C, et al. *Science Advances*. 2022. [DOI
    10.1126/sciadv.abq5914](https://doi.org/10.1126/sciadv.abq5914) (`MAD1L1`).
14. Abdel-Salam GMH, et al. *JCI Insight*. 2023. [PMID
    37796616](https://pubmed.ncbi.nlm.nih.gov/37796616/) (`MAD2L1BP`).
15. Guo J, et al. *Human Genetics and Genomics Advances*. 2024. [PMID
    37981762](https://pubmed.ncbi.nlm.nih.gov/37981762/) (`CEP192`).
16. Fujita Y, et al. *Aging Cell*. 2020. [PMID
    33094908](https://pubmed.ncbi.nlm.nih.gov/33094908/) (`CDC20`).

## Dataset citation

Andreoletti G et al. *Rare Disease, Real Kid. The 2026 MVA Hackathon*. Synapse, 2026. [doi:10.7303/SYN76251147](https://doi.org/10.7303/SYN76251147). Controlled-access inputs are not distributed with this repository.

## Acknowledgement

This work was made possible through the Hackathon, organized by Sage Bionetworks in partnership with the MVA Society, Hugging Face, and BEACON (The Benchmarking, Evaluation, and Assessment Consortium for Science), with prize sponsorship from AWS and Anthropic. We are deeply grateful to the child and their family who generously contributed their data and their story to advance research into this rare disease. We acknowledge their trust in making this Hackathon possible.
