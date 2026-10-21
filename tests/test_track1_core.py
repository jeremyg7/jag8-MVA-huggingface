from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
import zipfile
import gzip


REPO = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("track1", REPO / "scripts/track1.py")
assert SPEC and SPEC.loader
track1 = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(track1)


def base_row(**overrides):
    row = {
        "CHROM": "1",
        "POS": "100",
        "REF": "A",
        "ALT": "G",
        "FILTER": "PASS",
        "GT": "0/1",
        "DP": "30",
        "GQ": "60",
        "PS": "",
        "SYMBOL": "GENE1",
        "Gene": "ENSG1",
        "Consequence": "missense_variant",
        "IMPACT": "MODERATE",
        "MANE_SELECT": "NM_TEST.1",
        "CANONICAL": "YES",
        "MAX_AF": "0.0001",
        "gnomADe_AF": "0.0001",
        "gnomADg_AF": "0.00005",
        "CLIN_SIG": "",
        "Existing_variation": "",
    }
    row.update(overrides)
    return row


class HpoTests(unittest.TestCase):
    def test_exact_hpo_extraction_preserves_order_and_deduplicates(self):
        terms = [f"HP:{index:07d}" for index in range(1, 9)]
        with tempfile.TemporaryDirectory() as directory:
            docx = Path(directory) / "phenotype.docx"
            with zipfile.ZipFile(docx, "w") as archive:
                archive.writestr(
                    "word/document.xml",
                    "<w:document>" + " ".join(terms + [terms[2]]) + "</w:document>",
                )
            self.assertEqual(track1.extract_hpo_docx(docx), terms)

    def test_generated_exomiser_job_uses_only_supplied_terms(self):
        terms = [f"HP:{index:07d}" for index in range(1, 9)]
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "job.yml"
            track1.generate_exomiser_job(REPO, output, sample_id="SAMPLE", hpo_ids=terms)
            text = output.read_text(encoding="utf-8")
            self.assertEqual(sum(text.count(term) for term in terms), 8)
            self.assertNotIn("genePanelFilter", text)
            self.assertIn("analysisMode: PASS_ONLY", text)


class DiseasePanelTests(unittest.TestCase):
    def test_panel_is_literature_layer_with_equal_canonical_tier(self):
        panel = track1.load_panel(REPO / "config/disease-panels/mva.yml")
        canonical = [row for row in panel["genes"] if row["tier"] == "canonical_mva"]
        self.assertGreaterEqual(len(canonical), 4)
        self.assertEqual({row["tier"] for row in canonical}, {"canonical_mva"})
        self.assertIn("BUB1B", {row["symbol"] for row in canonical})
        text = (REPO / "config/disease-panels/mva.yml").read_text(encoding="utf-8").lower()
        self.assertNotIn("leaderboard", text)
        self.assertNotIn("target_gene", text)

    def test_panel_gff_selection_emits_padded_chr_bed(self):
        panel = track1.load_panel(REPO / "config/disease-panels/mva.yml")
        panel = {"genes": [panel["genes"][0]], "gene_lookup": panel["gene_lookup"]}
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "tiny.gff3.gz"
            with gzip.open(source, "wt", encoding="utf-8") as handle:
                handle.write(
                    "##gff-version 3\n"
                    "1\tEnsembl\tgene\t100\t200\t.\t+\t.\tID=gene:ENSG1;Name=BUB1B\n"
                    "1\tEnsembl\tmRNA\t100\t200\t.\t+\t.\tID=transcript:ENST1;Parent=gene:ENSG1\n"
                    "1\tEnsembl\tCDS\t110\t150\t.\t+\t0\tID=cds:CDS1;Parent=transcript:ENST1\n"
                )
            result = track1.panel_gff_and_intervals(
                gff_path=source,
                panel=panel,
                vcf_contigs={"1": 1000},
                destination_gff=root / "panel.gff3.gz",
                destination_bed=root / "panel.bed",
                padding_bp=20,
            )
            self.assertEqual(result["genes"], ["BUB1B"])
            self.assertEqual((root / "panel.bed").read_text(encoding="utf-8"), "1\t79\t220\n")
            self.assertIn("chr1", gzip.open(root / "panel.gff3.gz", "rt", encoding="utf-8").read())

    def test_panel_gff_selection_keeps_one_interval_per_gene(self):
        panel = track1.load_panel(REPO / "config/disease-panels/mva.yml")
        genes = [panel["genes"][0], panel["genes"][1]]
        panel = {
            "genes": genes,
            "gene_lookup": {name: item for item in genes for name in [item["symbol"], *item["aliases"]]},
        }
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "tiny.gff3.gz"
            with gzip.open(source, "wt", encoding="utf-8") as handle:
                handle.write(
                    "##gff-version 3\n"
                    "1\tEnsembl\tgene\t100\t200\t.\t+\t.\tID=gene:ENSG1;Name=BUB1B\n"
                    "1\tEnsembl\tgene\t1000\t1100\t.\t+\t.\tID=gene:ENSG2;Name=CEP57\n"
                )
            result = track1.panel_gff_and_intervals(
                gff_path=source,
                panel=panel,
                vcf_contigs={"1": 2000},
                destination_gff=root / "panel.gff3.gz",
                destination_bed=root / "panel.bed",
                padding_bp=20,
            )
            self.assertEqual(result["interval_count"], 2)
            self.assertEqual(
                (root / "panel.bed").read_text(encoding="utf-8"),
                "1\t79\t220\n1\t979\t1120\n",
            )
            self.assertEqual(result["intervals_by_gene"]["BUB1B"]["start"], 80)
            self.assertEqual(result["intervals_by_gene"]["CEP57"]["start"], 980)

    def test_aliases_canonicalize_before_disease_architecture_grouping(self):
        panel = track1.load_panel(REPO / "config/disease-panels/mva.yml")
        rows = [
            base_row(SYMBOL="BUBR1", Gene="BUBR1", POS="100"),
            base_row(SYMBOL="BUBR1", Gene="BUBR1", POS="200", ALT="T"),
        ]
        classified = track1.classify_vep_rows(rows, self_thresholds())
        architectures = track1.build_disease_architectures(classified["primary"], panel)
        self.assertEqual(len(architectures), 1)
        self.assertEqual(architectures[0]["gene"], "BUB1B")
        self.assertEqual(architectures[0]["architecture"], "compound_heterozygous")

    def test_unresolved_primary_gene_fails_loudly(self):
        panel = track1.load_panel(REPO / "config/disease-panels/mva.yml")
        classified = track1.classify_vep_rows(
            [base_row(SYMBOL="NOT_A_PANEL_GENE", Gene="NOT_A_PANEL_GENE")], self_thresholds()
        )
        with self.assertRaises(track1.PipelineError):
            track1.build_disease_architectures(classified["primary"], panel)


class SubmissionTests(unittest.TestCase):
    def test_pair_submission_uses_official_columns_and_chr_prefix(self):
        panel = track1.load_panel(REPO / "config/disease-panels/mva.yml")
        first = base_row(CHROM="chr1", POS="100", SYMBOL="BUB1B", Gene="ENSG1")
        second = base_row(CHROM="chr1", POS="200", ALT="T", SYMBOL="BUB1B", Gene="ENSG1")
        classified = track1.classify_vep_rows([first, second], self_thresholds())
        architectures = track1.build_disease_architectures(classified["primary"], panel)
        ranked = track1.rank_disease_architectures(architectures, panel)
        self.assertTrue(ranked[0]["submission_eligible"])
        row = track1.architecture_submission_row(ranked[0], "PRIVATE", rank_index=0)
        self.assertEqual(list(row), track1.OFFICIAL_SUBMISSION_FIELDS)
        self.assertEqual(row["chrom_1"], "chr1")
        self.assertEqual(row["chrom_2"], "chr1")
        self.assertEqual(float(row["epcr"]), 1.0)

    def test_clarity_gate_rejects_single_recessive_allele(self):
        panel = track1.load_panel(REPO / "config/disease-panels/mva.yml")
        first = base_row(CHROM="chr1", SYMBOL="BUB1B", Gene="ENSG1")
        classified = track1.classify_vep_rows([first], self_thresholds())
        ranked = track1.rank_disease_architectures(
            track1.build_disease_architectures(classified["primary"], panel), panel
        )
        self.assertFalse(track1.submission_clarity(ranked, panel)[0])


def self_thresholds():
    return {
        "minimum_dp": 10,
        "minimum_gq": 20,
        "compound_het_max_af": 0.01,
        "single_or_hom_max_af": 0.001,
    }


class StorageTests(unittest.TestCase):
    def config_for(self, volume: Path, minimum: int = 0):
        project = volume / "MVA"
        return {
            "storage": {
                "volume_root": str(volume),
                "project_root": str(project),
                "reference_root": str(project / "reference"),
                "private_run_root": str(project / "private/track1"),
                "reference_tmp": str(project / "reference/tmp"),
                "run_tmp": str(project / "private/track1/tmp"),
                "docker_disk_root": str(project / "reference/containers/docker-desktop"),
                "minimum_setup_free_gib": minimum,
                "minimum_run_free_gib": minimum,
            }
        }

    def test_valid_external_tree_and_create(self):
        with tempfile.TemporaryDirectory() as directory:
            volume = Path(directory)
            result = track1.validate_storage(self.config_for(volume), phase="setup", create=True)
            self.assertEqual(result["required_gib"], 0)
            self.assertTrue((volume / "MVA/reference/tmp").is_dir())

    def test_path_escape_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            volume = Path(directory)
            config = self.config_for(volume)
            config["storage"]["run_tmp"] = str(volume.parent / "escape")
            with self.assertRaises(track1.PipelineError):
                track1.validate_storage(config, phase="setup")

    @unittest.skipUnless(shutil.which("curl"), "curl required")
    def test_parallel_download_assembles_ranges_and_resumes_first_part(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.bin"
            payload = bytes(range(256)) * 8192
            source.write_bytes(payload)
            destination = root / "result.part"
            destination.write_bytes(payload[:100_000])
            parts_dir = track1.parallel_download(
                url=source.as_uri(),
                destination=destination,
                expected_bytes=len(payload),
                tmp_path=root,
                segment_size=262_144,
                workers=4,
            )
            self.assertEqual(destination.read_bytes(), payload)
            self.assertTrue(parts_dir.is_dir())

    def test_symlink_escape_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory, tempfile.TemporaryDirectory() as outside:
            volume = Path(directory)
            project = volume / "MVA"
            project.mkdir()
            (project / "reference").symlink_to(Path(outside), target_is_directory=True)
            config = self.config_for(volume)
            with self.assertRaises(track1.PipelineError):
                track1.validate_storage(config, phase="setup")

    def test_insufficient_space_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            config = self.config_for(Path(directory), minimum=10**9)
            with self.assertRaises(track1.PipelineError):
                track1.validate_storage(config, phase="setup")


class ClassificationTests(unittest.TestCase):
    thresholds = {
        "minimum_dp": 10,
        "minimum_gq": 20,
        "compound_het_max_af": 0.01,
        "single_or_hom_max_af": 0.001,
    }

    def test_compound_het_phase_unknown_and_primary(self):
        rows = [base_row(), base_row(POS="200", ALT="T")]
        result = track1.classify_vep_rows(rows, self.thresholds)
        architectures = track1.build_primary_architectures(result["primary"])
        pairs = [row for row in architectures if row["architecture"] == "compound_heterozygous"]
        self.assertEqual(len(pairs), 1)
        self.assertEqual(pairs[0]["phase_status"], "phase_unknown")

    def test_phase_relationship_supported_trans_and_cis(self):
        first = {"GT": "0|1", "PS": "42"}
        trans = {"GT": "1|0", "PS": "42"}
        cis = {"GT": "0|1", "PS": "42"}
        wrong_block = {"GT": "1|0", "PS": "99"}
        self.assertEqual(track1.phase_relationship(first, trans), "in_trans_supported")
        self.assertEqual(track1.phase_relationship(first, cis), "in_cis")
        self.assertEqual(track1.phase_relationship(first, wrong_block), "phase_unknown")

    def test_homozygous_and_dominant_architectures(self):
        rows = [
            base_row(GT="1/1", POS="300"),
            base_row(SYMBOL="GENE2", Gene="ENSG2", POS="400", MAX_AF="0.00001"),
        ]
        result = track1.classify_vep_rows(rows, self.thresholds)
        architectures = track1.build_primary_architectures(result["primary"])
        self.assertIn("homozygous_alt", {row["architecture"] for row in architectures})
        self.assertIn("single_heterozygous", {row["architecture"] for row in architectures})

    def test_quality_rescue_is_separate_and_pairs_with_primary(self):
        rows = [base_row(), base_row(POS="200", FILTER="LowQual", DP="6", GQ="12")]
        result = track1.classify_vep_rows(rows, self.thresholds)
        self.assertEqual(len(result["primary"]), 1)
        self.assertEqual(len(result["rescue"]), 1)
        rescue_architectures = track1.build_rescue_architectures(result["primary"], result["rescue"])
        self.assertEqual(len(rescue_architectures), 1)
        self.assertEqual(rescue_architectures[0]["rescue_status"], "primary_plus_rescue")

    def test_two_rescue_alleles_form_separate_architecture(self):
        rows = [
            base_row(FILTER="LowQual"),
            base_row(POS="200", FILTER="LowQual"),
        ]
        result = track1.classify_vep_rows(rows, self.thresholds)
        architectures = track1.build_rescue_architectures(result["primary"], result["rescue"])
        self.assertEqual(len(architectures), 1)
        self.assertEqual(architectures[0]["rescue_status"], "rescue_only")

    def test_clinvar_pathogenic_quality_rescue_overrides_frequency(self):
        rows = [
            base_row(
                FILTER="LowQual",
                MAX_AF="0.2",
                gnomADe_AF="0.2",
                CLIN_SIG="likely_pathogenic",
                IMPACT="LOW",
                Consequence="synonymous_variant",
            )
        ]
        result = track1.classify_vep_rows(rows, self.thresholds)
        self.assertEqual(len(result["rescue"]), 1)
        self.assertTrue(result["rescue"][0]["clinvar_plp"])

    def test_missing_dp_gq_and_af_are_retained_and_flagged(self):
        result = track1.classify_vep_rows(
            [base_row(DP=".", GQ=".", MAX_AF=".", gnomADe_AF=".", gnomADg_AF=".")],
            self.thresholds,
        )
        self.assertEqual(len(result["primary"]), 1)
        self.assertTrue(result["primary"][0]["af_missing"])
        self.assertEqual(result["primary"][0]["quality_status"], "not_evaluable")


class FunnelAndFreezeTests(unittest.TestCase):
    def test_funnel_invariants(self):
        good = [
            {"stage_id": "a", "unit": "allele", "count": 5, "parent_stage": ""},
            {"stage_id": "b", "unit": "allele", "count": 3, "parent_stage": "a"},
        ]
        track1.validate_funnel(good)
        bad = [
            {"stage_id": "a", "unit": "allele", "count": 5, "parent_stage": ""},
            {"stage_id": "b", "unit": "allele", "count": 6, "parent_stage": "a"},
        ]
        with self.assertRaises(track1.PipelineError):
            track1.validate_funnel(bad)

    def test_bias_guard_rejects_targeting_construct(self):
        with tempfile.TemporaryDirectory() as directory:
            clean = Path(directory) / "clean.yml"
            clean.write_text("analysisMode: PASS_ONLY\n", encoding="utf-8")
            self.assertTrue(track1.bias_guard([clean])["passed"])
            contaminated = Path(directory) / "bad.yml"
            contaminated.write_text("genePanelFilter: {}\n", encoding="utf-8")
            with self.assertRaises(track1.PipelineError):
                track1.bias_guard([contaminated])

    def test_checksums_detect_mutation(self):
        with tempfile.TemporaryDirectory() as directory:
            run_dir = Path(directory)
            artifact = run_dir / "artifact.tsv"
            artifact.write_text("a\n", encoding="utf-8")
            track1.write_checksums(run_dir)
            track1.validate_checksums(run_dir)
            artifact.write_text("changed\n", encoding="utf-8")
            with self.assertRaises(track1.PipelineError):
                track1.validate_checksums(run_dir)

    def test_targeted_confirmation_refuses_without_freeze(self):
        with tempfile.TemporaryDirectory() as directory:
            volume = Path(directory)
            project = volume / "MVA"
            private = project / "private/track1"
            private.mkdir(parents=True)
            config = StorageTests().config_for(volume)
            with self.assertRaises(track1.PipelineError):
                track1.run_confirmation(config, unbiased_run="missing", gene="GENE1", dry_run=True)

    def test_download_checkpoint_records_verified_stop(self):
        with tempfile.TemporaryDirectory() as directory:
            reference_root = Path(directory)
            record = {
                "name": "archive.zip",
                "expected_bytes": 11,
                "bytes": 11,
                "sha256": "abc123",
                "path": str(reference_root / "archive.zip"),
            }
            path = track1.write_download_checkpoint(reference_root, record)
            checkpoint = track1.load_json(path)
            self.assertEqual(checkpoint["stopped_after"], "archive.zip")
            self.assertTrue(checkpoint["size_verified"])
            self.assertTrue(checkpoint["checksum_verified"])
            self.assertFalse(checkpoint["downstream_started"])


@unittest.skipUnless(shutil.which("bcftools") and shutil.which("bgzip") and shutil.which("tabix"), "HTS tools required")
class NormalizationTests(unittest.TestCase):
    def test_normalization_splits_multiallelic_and_preserves_formats(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            fasta = root / "reference.fa"
            fasta.write_text(">chr1\nAAAAACCCCCAAAAACCCCC\n", encoding="utf-8")
            fai = root / "reference.fa.fai"
            fai.write_text("chr1\t20\t6\t20\t21\n", encoding="utf-8")
            vcf = root / "source.vcf"
            vcf.write_text(
                "##fileformat=VCFv4.2\n"
                "##contig=<ID=1,length=20>\n"
                "##FORMAT=<ID=GT,Number=1,Type=String,Description=\"Genotype\">\n"
                "##FORMAT=<ID=AD,Number=R,Type=Integer,Description=\"Allele depth\">\n"
                "#CHROM\tPOS\tID\tREF\tALT\tQUAL\tFILTER\tINFO\tFORMAT\tSAMPLE\n"
                "1\t2\t.\tA\tC,G\t60\tPASS\t.\tGT:AD\t1/2:5,3,4\n"
                "1\t7\t.\tCC\tC\t60\tPASS\t.\tGT:AD\t0/1:10,6\n",
                encoding="utf-8",
            )
            compressed = root / "source.vcf.gz"
            with compressed.open("wb") as output:
                subprocess.run(["bgzip", "-c", str(vcf)], stdout=output, check=True)
            subprocess.run(["tabix", "-p", "vcf", str(compressed)], check=True)
            run_dir = root / "run"
            run_dir.mkdir()
            summary = track1.normalize_vcf(
                original=compressed,
                fasta=fasta,
                fai=fai,
                run_dir=run_dir,
                tmp_path=root,
                expected_samples=1,
            )
            self.assertEqual(summary["canonical_biallelic_records"], 3)
            self.assertTrue(summary["ad_sum_preservation_requested"])
            output = subprocess.run(
                ["bcftools", "query", "-f", "%CHROM\t%POS\t%REF\t%ALT[\t%GT\t%AD]\n", str(summary["canonical"])],
                text=True,
                capture_output=True,
                check=True,
            ).stdout
            self.assertNotIn("C,G", output)
            self.assertIn("1\t5\tAC\tA", output)


@unittest.skipUnless(shutil.which("bcftools") and shutil.which("bgzip") and shutil.which("tabix"), "HTS tools required")
class LocalCsqIntegrationTests(unittest.TestCase):
    def test_real_bcftools_csq_splice_alias_reaches_architecture(self):
        panel = track1.load_panel(REPO / "config/disease-panels/mva.yml")
        b1 = panel["genes"][0]
        panel = {
            "genes": [b1],
            "gene_lookup": {name: b1 for name in [b1["symbol"], *b1["aliases"]]},
        }
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            fasta = root / "reference.fa"
            fasta.write_text(">chr1\n" + ("A" * 1000) + "\n", encoding="utf-8")
            (root / "reference.fa.fai").write_text("chr1\t1000\t6\t1000\t1001\n", encoding="utf-8")
            gff = root / "annotation.gff3.gz"
            with gzip.open(gff, "wt", encoding="utf-8") as handle:
                handle.write(
                    "##gff-version 3\n"
                    "chr1\tEnsembl\tgene\t100\t300\t.\t+\t.\tID=gene:ENSG1;Name=BUB1B\n"
                    "chr1\tEnsembl\tmRNA\t100\t300\t.\t+\t.\tID=transcript:ENST1;Parent=gene:ENSG1\n"
                    "chr1\tEnsembl\texon\t100\t150\t.\t+\t.\tID=exon:E1;Parent=transcript:ENST1\n"
                    "chr1\tEnsembl\texon\t200\t300\t.\t+\t.\tID=exon:E2;Parent=transcript:ENST1\n"
                    "chr1\tEnsembl\tCDS\t100\t150\t.\t+\t0\tID=cds:CDS1;Parent=transcript:ENST1\n"
                    "chr1\tEnsembl\tCDS\t200\t300\t.\t+\t0\tID=cds:CDS2;Parent=transcript:ENST1\n"
                )
            vcf = root / "input.vcf"
            vcf.write_text(
                "##fileformat=VCFv4.3\n"
                "##contig=<ID=chr1,length=1000>\n"
                "##FORMAT=<ID=GT,Number=1,Type=String,Description=\"Genotype\">\n"
                "##FORMAT=<ID=DP,Number=1,Type=Integer,Description=\"Depth\">\n"
                "##FORMAT=<ID=GQ,Number=1,Type=Integer,Description=\"Quality\">\n"
                "#CHROM\tPOS\tID\tREF\tALT\tQUAL\tFILTER\tINFO\tFORMAT\tSAMPLE\n"
                "chr1\t151\t.\tA\tG\t60\tPASS\t.\tGT:DP:GQ\t0/1:30:60\n"
                "chr1\t152\t.\tA\tC\t60\tPASS\t.\tGT:DP:GQ\t0/1:30:60\n",
                encoding="utf-8",
            )
            compressed = root / "input.vcf.gz"
            with compressed.open("wb") as output:
                subprocess.run(["bgzip", "-c", str(vcf)], stdout=output, check=True)
            subprocess.run(["tabix", "-p", "vcf", str(compressed)], check=True)
            csq = root / "csq.vcf.gz"
            subprocess.run(
                [
                    "bcftools",
                    "csq",
                    "--local-csq",
                    "-f",
                    str(fasta),
                    "-g",
                    str(gff),
                    "-Oz",
                    "-o",
                    str(csq),
                    str(compressed),
                ],
                check=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            rows = track1.parse_csquery(
                csq,
                panel_gene_ids={"BUB1B": "ENSG1"},
                panel_gene_lookup=panel["gene_lookup"],
                command_log=root / "commands.log",
                tmp_path=root,
            )
            self.assertTrue(rows)
            self.assertTrue(any(row["Consequence"] == "splice_donor_variant" for row in rows))
            self.assertTrue(all(row["IMPACT"] == "HIGH" for row in rows))
            classified = track1.classify_vep_rows(rows, self_thresholds())
            self.assertEqual(len(classified["primary"]), 2)
            architectures = track1.build_disease_architectures(classified["primary"], panel)
            self.assertTrue(any(row["architecture"] == "compound_heterozygous" for row in architectures))


if __name__ == "__main__":
    unittest.main()
