"""Historical importer fixtures. No network and no API collector execution."""
import copy
from datetime import datetime
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest

import openpyxl

ROOT=Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location('historical_import',ROOT/'scripts/import_historical_excel.py')
m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)

def fixture(path,metric_value=0):
    book=openpyxl.Workbook();summary=book.active;summary.title='Resumo Trimestral'
    for i,value in enumerate(m.SUMMARY_HEADERS,2):summary.cell(3,i,value)
    summary.cell(4,1,'2026 Q3');summary.cell(5,1,'TOTAL')
    for group,config in m.GROUPS.items():
        sheet=book.create_sheet(config['sheet'])
        sheet.append(['Title']);sheet.append(m.HEADERS[group])
        values=['2026 Q3','18012345678901234','09/13/2026 12:35','Reels' if group=='facebook' else 'Story do Instagram' if group=='instagram-stories' else 'Reel do Instagram','Título de teste']+[metric_value]*(config['link']-5)+['https://www.facebook.com/posts/test' if group=='facebook' else 'https://www.instagram.com/p/test/']
        sheet.append(values)
        for metric,index in m.SUMMARY[group].items():
            for row in (4,5):summary.cell(row,index+1,1 if metric=='count' else metric_value)
    book.save(path)

class ImportTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.path=Path(self.temp.name)/'source.xlsx';fixture(self.path)

    def edit(self,fn):
        book=openpyxl.load_workbook(self.path);fn(book);book.save(self.path)

    def test_real_contract_fixture_matches_all_summary_totals(self):
        result=m.import_workbook(self.path)
        self.assertEqual(result['validation']['summary']['checks'],40)
        self.assertEqual(result['validation']['summary']['matched'],40)
        self.assertEqual(result['validation']['summary']['divergences'],0)

    def test_zeros_preserved_and_ids_never_float_converted(self):
        record=m.import_workbook(self.path)['datasets']['instagram-posts'][0]
        self.assertEqual(record['metrics']['views'],0)
        self.assertEqual(record['id'],'18012345678901234')
        self.assertEqual(record['published_at'],'2026-09-13T12:35')
        self.assertEqual(record['provenance']['timezone'],'not_specified_in_workbook')
        self.assertEqual(record['url'],'https://www.instagram.com/p/test/')

    def test_missing_value_stays_null_with_coverage_and_divergence(self):
        self.edit(lambda w:setattr(w['Instagram Posts']['H3'],'value',None))
        result=m.import_workbook(self.path)
        self.assertIsNone(result['datasets']['instagram-posts'][0]['metrics']['views'])
        check=next(c for c in result['validation']['checks'] if c['group']=='instagram-posts' and c['metric']=='views')
        self.assertEqual(check['known'],0);self.assertEqual(check['status'],'divergence')

    def test_negative_boolean_and_fractional_metrics_not_zeroed(self):
        for value in (-1,True,1.5):
            self.edit(lambda w:setattr(w['Facebook']['J3'],'value',value))
            result=m.import_workbook(self.path)
            self.assertIsNone(result['datasets']['facebook'][0]['metrics']['views'])
            self.assertTrue(any(i['category']=='invalid_metric' for i in result['validation']['issues']))

    def test_duplicate_identical_deduplicates_and_retains_rows(self):
        self.edit(lambda w:w['Facebook'].append([c.value for c in w['Facebook'][3]]))
        result=m.import_workbook(self.path)
        self.assertEqual(len(result['datasets']['facebook']),1)
        self.assertEqual(result['datasets']['facebook'][0]['provenance']['rows'],[3,4])
        self.assertTrue(any(i['category']=='duplicate_identical' for i in result['validation']['issues']))

    def test_duplicate_conflict_quarantines_both_versions(self):
        def edit(w):
            row=[c.value for c in w['Facebook'][3]];row[9]=50;w['Facebook'].append(row)
        self.edit(edit);result=m.import_workbook(self.path)
        self.assertEqual(result['datasets']['facebook'],[])
        self.assertEqual(len(result['quarantine'][0]['versions']),2)
        self.assertGreater(result['validation']['summary']['divergences'],0)

    def test_different_ids_same_link_preserved_with_warning(self):
        def edit(w):
            row=[c.value for c in w['Facebook'][3]];row[1]='other';w['Facebook'].append(row)
        self.edit(edit);result=m.import_workbook(self.path)
        self.assertEqual(len(result['datasets']['facebook']),2)
        self.assertTrue(any(i['category']=='shared_link_distinct_ids' for i in result['validation']['issues']))

    def test_date_and_declared_quarter_mismatch_quarantined(self):
        self.edit(lambda w:setattr(w['Facebook']['A3'],'value','2026 Q2'))
        result=m.import_workbook(self.path)
        self.assertEqual(result['datasets']['facebook'],[])
        self.assertTrue(any(i['category']=='date_quarter_mismatch' for i in result['validation']['issues']))

    def test_ambiguous_text_date_not_guessed(self):
        self.edit(lambda w:setattr(w['Facebook']['C3'],'value','09/01/2026 12:35'))
        with self.assertRaisesRegex(ValueError,'ambiguous'):m.import_workbook(self.path)

    def test_excel_datetime_and_iso_dates_supported(self):
        for value in (datetime(2026,9,13,12,35),'2026-09-13T12:35'):
            self.edit(lambda w:setattr(w['Facebook']['C3'],'value',value))
            self.assertEqual(m.import_workbook(self.path)['datasets']['facebook'][0]['date'],'2026-09-13')

    def test_invalid_date_is_not_normalized(self):
        self.edit(lambda w:setattr(w['Facebook']['C3'],'value','09/31/2026 12:35'))
        result=m.import_workbook(self.path)
        self.assertEqual(result['datasets']['facebook'],[])

    def test_missing_sheet_and_shifted_headers_fail(self):
        self.edit(lambda w:setattr(w['Facebook']['J2'],'value','Impressões'))
        with self.assertRaisesRegex(ValueError,'header'):m.import_workbook(self.path)
        self.edit(lambda w:w.remove(w['Facebook']))
        with self.assertRaisesRegex(ValueError,'missing_required'):m.import_workbook(self.path)

    def test_summary_divergence_reported_not_overwritten(self):
        self.edit(lambda w:setattr(w['Resumo Trimestral']['G4'],'value',99))
        result=m.import_workbook(self.path)
        check=next(c for c in result['validation']['checks'] if c['period']=='2026 Q3' and c['group']=='facebook' and c['metric']=='views')
        self.assertEqual((check['declared'],check['calculated'],check['status']),(99,0,'divergence'))

    def test_unsafe_link_rejected_and_input_never_modified(self):
        self.edit(lambda w:setattr(w['Facebook']['M3'],'value','javascript:alert(1)'))
        before=self.path.read_bytes();result=m.import_workbook(self.path)
        self.assertEqual(result['datasets']['facebook'],[]);self.assertEqual(self.path.read_bytes(),before)

    def test_separate_output_and_source_hash(self):
        result=m.import_workbook(self.path);output=Path(self.temp.name)/'preview/data'
        m.write_outputs(result,output)
        self.assertTrue((output/'facebook.json').exists())
        self.assertTrue((output.parent/'VALIDACAO.md').exists())
        self.assertFalse(result['manifest']['source']['api_certified'])
        self.assertEqual(json.loads((output/'manifest.json').read_text(encoding='utf-8'))['source']['sha256'],m.hashlib.sha256(self.path.read_bytes()).hexdigest())

    def test_api_output_directory_is_protected(self):
        with self.assertRaisesRegex(ValueError,'protected_output'):m.write_outputs({},ROOT/'dados')

    def test_generated_real_data_reconciles_without_reimport(self):
        report=json.loads((ROOT/'historico/data/validation.json').read_text(encoding='utf-8'))
        self.assertEqual(report['summary']['checks'],240)
        self.assertEqual(report['summary']['divergences'],0)
        self.assertEqual(report['accepted_records'],{'facebook':1101,'instagram-posts':1812,'instagram-stories':6357})

if __name__=='__main__':unittest.main()
