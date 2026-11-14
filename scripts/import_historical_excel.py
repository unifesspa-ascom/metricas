#!/usr/bin/env python3
"""Import the official consolidated workbook into an isolated static preview.

Never reads or writes API JSONs. Zeros are explicit workbook values, not API
certifications. Workbook-local times have NO assumed timezone. Source text dates
are inferred sheet-wide only using unambiguous samples (MM/DD/YYYY in this file).
"""
import argparse
from collections import Counter, defaultdict
from datetime import date, datetime, timezone
import hashlib
import json
import math
from pathlib import Path
import re
import unicodedata
from urllib.parse import urlsplit, urlunsplit

import openpyxl
from openpyxl.utils import get_column_letter
from openpyxl.utils.datetime import from_excel

ROOT = Path(__file__).resolve().parents[1]
GROUPS = {
 'facebook': {'sheet':'Facebook','network':'facebook','metrics':{'reactions':5,'comments':6,'shares':7,'clicks':8,'views':9,'impressions':10,'engagement':11},'link':12},
 'instagram-posts': {'sheet':'Instagram Posts','network':'instagram','metrics':{'likes':5,'reach':6,'views':7,'shares':8,'saved':9,'comments':10,'follows':11,'engagement':12},'link':13},
 'instagram-stories': {'sheet':'Instagram Stories','network':'instagram','metrics':{'views':5,'reach':6,'likes':7,'replies':8,'navigation':9,'profile_visits':10,'follows':11},'link':12}}
SUMMARY = {'facebook':{'count':1,'reactions':2,'comments':3,'shares':4,'clicks':5,'views':6},'instagram-posts':{'count':7,'likes':8,'reach':9,'views':10,'shares':11,'saved':12,'follows':13},'instagram-stories':{'count':14,'views':15,'reach':16,'likes':17,'replies':18,'navigation':19,'follows':20}}
LABELS={'count':'Registros','views':'Visualizações','impressions':'Impressões','reach':'Soma dos alcances individuais','likes':'Curtidas','reactions':'Reações','comments':'Comentários','shares':'Compartilhamentos','clicks':'Cliques','saved':'Salvamentos','follows':'Seguimentos atribuídos','engagement':'Engajamento informado','replies':'Respostas','navigation':'Navegação','profile_visits':'Visitas ao perfil atribuídas'}
FORMATS={'reels':'reel','fotos':'image','videos':'video','imagem do instagram':'image','reel do instagram':'reel','carrossel do instagram':'carousel','story do instagram':'story'}
HEADERS={
 'facebook':['Trimestre','ID do post','Data de publicação','Tipo de post','Título / Descrição','Reações','Comentários','Compartilhamentos','Total de cliques','Visualizações','Impressões','Engajamento total','Link permanente'],
 'instagram-posts':['Trimestre','ID do post','Data de publicação','Tipo de post','Descrição','Curtidas','Alcance','Visualizações','Compartilhamentos','Salvamentos','Comentários','Seguimentos','Engajamento total','Link permanente'],
 'instagram-stories':['Trimestre','ID do post','Data de publicação','Tipo de post','Descrição','Visualizações','Alcance','Curtidas','Respostas','Navegação','Visitas ao perfil','Seguimentos','Link permanente']}
SUMMARY_HEADERS=['Posts','Reações','Comentários','Compart.','Cliques','Views','Posts','Curtidas','Alcance','Views','Compart.','Salvam.','Seguim.','Stories','Views','Alcance','Curtidas','Respostas','Navegação','Seg.']

def norm(value):
 return ''.join(c for c in unicodedata.normalize('NFKD',str(value).lower()) if not unicodedata.combining(c)).strip()

def period(value):
 match=re.fullmatch(r'(202[456])\s+Q([1-4])',str(value or '').strip())
 if not match:return None
 year,quarter=map(int,match.groups())
 return f'{year} Q{quarter}' if year<2026 or quarter<=3 else None

def number(value):
 if value is None or value=='':return None
 if type(value) is int and value>=0:return value
 if type(value) is float and math.isfinite(value) and value>=0 and value.is_integer():return int(value)
 if isinstance(value,str) and re.fullmatch(r'\d+',value.strip()):return int(value.strip())
 raise ValueError('invalid_nonnegative_count')

def date_order(rows):
 orders=set()
 for row in rows:
  value=row[2].value
  if not isinstance(value,str):continue
  match=re.match(r'^(\d{1,2})/(\d{1,2})/\d{4}',value)
  if match:
   a,b=map(int,match.groups())
   if a>12:orders.add('DMY')
   if b>12:orders.add('MDY')
 if len(orders)!=1:raise ValueError('ambiguous_or_mixed_date_order')
 return orders.pop()

def publication_date(cell,order,epoch):
 value=cell.value
 if isinstance(value,datetime):return value
 if isinstance(value,date):return datetime.combine(value,datetime.min.time())
 if type(value)in(int,float) and cell.is_date:return from_excel(value,epoch)
 if not isinstance(value,str):raise ValueError('invalid_publication_date')
 value=value.strip()
 if re.fullmatch(r'\d{4}-\d{2}-\d{2}(?:[ T]\d{2}:\d{2}(?::\d{2})?)?',value):return datetime.fromisoformat(value)
 fmt='%m/%d/%Y' if order=='MDY' else '%d/%m/%Y'
 for ending in (' %H:%M:%S',' %H:%M',''):
  try:return datetime.strptime(value,fmt+ending)
  except ValueError:pass
 raise ValueError('invalid_publication_date')

def safe_link(value):
 if not value:return None
 if not isinstance(value,str):raise ValueError('invalid_link')
 p=urlsplit(value.strip())
 allowed=('facebook.com','www.facebook.com','m.facebook.com','fb.watch','instagram.com','www.instagram.com')
 if p.scheme!='https' or p.hostname not in allowed or p.username or p.password:raise ValueError('unsafe_link')
 if any(key in p.query.lower() for key in ('access_token=','password=','secret=')):raise ValueError('unsafe_link')
 return value.strip()

def source_id(cell):
 value=cell.value
 if isinstance(value,str) and re.fullmatch(r'[A-Za-z0-9_-]+',value.strip()):return value.strip(),False
 if type(value)is int and value>=0:return str(value),value>2**53-1
 if type(value)is float and math.isfinite(value) and value>=0 and value.is_integer():return str(int(value)),True
 raise ValueError('invalid_identifier')

def import_workbook(path):
 path=Path(path);source_hash=hashlib.sha256(path.read_bytes()).hexdigest()
 workbook=openpyxl.load_workbook(path,data_only=True)
 required={'Resumo Trimestral'}|{v['sheet'] for v in GROUPS.values()}
 if not required<=set(workbook.sheetnames):raise ValueError('missing_required_sheet')
 issues=[];quarantine=[];datasets={};orders={};row_counts={};cross_ids=defaultdict(set)
 def issue(kind,sheet,row,**extra):issues.append({'category':kind,'sheet':sheet,'row':row,**extra})
 for group,config in GROUPS.items():
  sheet=workbook[config['sheet']];rows=[list(row) for row in sheet.iter_rows(min_row=3) if any(c.value is not None for c in row)]
  # A changed schema must fail loudly, never silently shift metric columns.
  expected=HEADERS[group]
  if [norm(sheet.cell(2,i+1).value) for i in range(len(expected))]!=[norm(v) for v in expected]:raise ValueError('unexpected_sheet_header: '+sheet.title)
  order=date_order(rows) if any(isinstance(row[2].value,str) and '/' in row[2].value for row in rows) else 'ISO_OR_EXCEL'
  orders[group]=order;row_counts[group]=len(rows);records={};blocked=set();link_ids=defaultdict(list)
  for row in rows:
   n=row[0].row;declared=period(row[0].value)
   try:
    if not declared:raise ValueError('unsupported_period')
    pid,precision=source_id(row[1]);published=publication_date(row[2],order,workbook.epoch)
    if published.tzinfo is not None:raise ValueError('unexpected_timezone_requires_review')
    actual=f'{published.year} Q{(published.month-1)//3+1}'
    if actual!=declared:raise ValueError('date_quarter_mismatch')
    url_value=row[config['link']].value
    if not url_value and row[config['link']].hyperlink:url_value=row[config['link']].hyperlink.target
    url=safe_link(url_value)
   except (ValueError,TypeError,OverflowError) as error:
    issue(str(error),sheet.title,n)
    quarantine.append({'group':group,'sheet':sheet.title,'row':n,'reason':str(error),'values':[str(c.value) if c.value is not None else None for c in row]})
    continue
   metrics={};cells={}
   for metric,index in config['metrics'].items():
    cells[metric]=f'{get_column_letter(index+1)}{n}'
    try:metrics[metric]=number(row[index].value)
    except ValueError:metrics[metric]=None;issue('invalid_metric',sheet.title,n,metric=metric,cell=cells[metric])
   record={'id':pid,'group':group,'network':config['network'],'date':published.date().isoformat(),'published_at':published.isoformat(timespec='minutes'),'year':published.year,'quarter':(published.month-1)//3+1,'period':declared,'format':FORMATS.get(norm(row[3].value),'unknown'),'source_format':str(row[3].value or ''),'title':str(row[4].value or ''),'url':url,'metrics':metrics,'provenance':{'sheet':sheet.title,'rows':[n],'id_cell':f'B{n}','date_cell':f'C{n}','metric_cells':cells,'timezone':'not_specified_in_workbook'}}
   if precision:issue('numeric_identifier_precision_unverifiable',sheet.title,n);record['identifier_precision']='source_numeric_unverifiable'
   if pid in records:
    previous=records[pid];a={k:v for k,v in previous.items() if k!='provenance'};b={k:v for k,v in record.items() if k!='provenance'}
    if a==b:
     previous['provenance']['rows'].append(n);issue('duplicate_identical',sheet.title,n,id=pid)
    else:
     blocked.add(pid);issue('duplicate_conflict_quarantined',sheet.title,n,id=pid)
     quarantine.append({'group':group,'id':pid,'reason':'duplicate_conflict','versions':[previous,record]})
   else:records[pid]=record
   if url:link_ids[urlunsplit((*urlsplit(url)[:3],'',''))].append(pid)
   cross_ids[(config['network'],pid)].add(group)
  for url,ids in link_ids.items():
   if len(set(ids))>1:issue('shared_link_distinct_ids',sheet.title,None,ids=sorted(set(ids)))
  datasets[group]=sorted([record for pid,record in records.items() if pid not in blocked],key=lambda r:(r['date'],r['published_at'],r['id']))
 for (network,pid),groups in cross_ids.items():
  if len(groups)>1:issue('identifier_in_multiple_formats',network,None,id=pid,groups=sorted(groups))
 summary=[];checks=[]
 sheet=workbook['Resumo Trimestral']
 if [norm(sheet.cell(3,i+2).value) for i in range(20)]!=[norm(v) for v in SUMMARY_HEADERS]:raise ValueError('unexpected_summary_header')
 for row in sheet.iter_rows(min_row=4):
  raw=row[0].value
  if raw is None:continue
  label=period(raw)
  if not label and str(raw).strip()!='TOTAL':issue('unsupported_summary_period',sheet.title,row[0].row);continue
  totals=str(raw).strip()=='TOTAL'
  for group,mapping in SUMMARY.items():
   selected=[r for r in datasets[group] if totals or r['period']==label]
   values={};coverage={};declared={}
   for metric,index in mapping.items():
    try:expected_value=number(row[index].value)
    except ValueError:expected_value=None;issue('invalid_summary_value',sheet.title,row[0].row,metric=metric)
    known=len(selected) if metric=='count' else sum(r['metrics'].get(metric)is not None for r in selected)
    calculated=len(selected) if metric=='count' else sum(r['metrics'][metric] for r in selected if r['metrics'].get(metric)is not None) if known else None
    status='match' if expected_value is not None and expected_value==calculated and known==len(selected) else 'unavailable' if expected_value is None else 'divergence'
    checks.append({'period':'TOTAL' if totals else label,'group':group,'metric':metric,'declared':expected_value,'calculated':calculated,'known':known,'records':len(selected),'status':status,'source_cell':f'{get_column_letter(index+1)}{row[0].row}'})
    values[metric]=calculated;declared[metric]=expected_value;coverage[metric]={'known':known,'records':len(selected)}
   if not totals:summary.append({'period':label,'year':int(label[:4]),'quarter':int(label[-1]),'group':group,'metrics':values,'declared_metrics':declared,'coverage':coverage,'provenance':{'sheet':sheet.title,'row':row[0].row}})
 periods=sorted({r['period'] for r in summary})
 report={'schema_version':1,'source_file':path.name,'source_sha256':source_hash,'input_rows':row_counts,'accepted_records':{k:len(v) for k,v in datasets.items()},'date_orders':orders,'checks':checks,'issues':issues,'quarantined_items':len(quarantine),'summary':{'checks':len(checks),'matched':sum(c['status']=='match' for c in checks),'divergences':sum(c['status']=='divergence' for c in checks),'unavailable':sum(c['status']=='unavailable' for c in checks),'duplicate_rows':sum(i['category'].startswith('duplicate') for i in issues)}}
 manifest={'schema_version':1,'source':{'kind':'official_consolidated_workbook','file':path.name,'sha256':source_hash,'api_certified':False},'imported_at':datetime.now(timezone.utc).isoformat(),'periods':periods,'groups':list(GROUPS),'record_counts':{k:len(v) for k,v in datasets.items()},'metric_labels':LABELS,'validation':report['summary'],'methodology':['Fonte histórica consolidada em Excel, independente das coletas automáticas da Meta.','Datas textuais interpretadas no formato identificado por aba; nenhum fuso foi presumido.','Zeros representam valores explicitamente informados na planilha; campos vazios são n/d.','Alcance é informado por publicação: sua soma não representa alcance único da conta.','Visualizações e impressões permanecem métricas distintas.','Resultados individuais não comprovam visualizações ocorridas exclusivamente no trimestre de publicação.','Comparações anuais usam trimestres comuns; 2026 contém somente o primeiro ao terceiro trimestre.']}
 return {'manifest':manifest,'summary':summary,'datasets':datasets,'validation':report,'quarantine':quarantine}

def write_outputs(result,output):
 output=Path(output).resolve()
 if output==ROOT or output.is_relative_to(ROOT/'dados') or output.is_relative_to(ROOT/'.github'):raise ValueError('protected_output_directory')
 output.mkdir(parents=True,exist_ok=True)
 payloads={'manifest.json':result['manifest'],'summary.json':{'schema_version':1,'source_sha256':result['manifest']['source']['sha256'],'periods':result['summary']},'validation.json':result['validation'],'quarantine.json':result['quarantine']}
 payloads.update({group+'.json':{'schema_version':1,'source_sha256':result['manifest']['source']['sha256'],'records':records} for group,records in result['datasets'].items()})
 for name,data in payloads.items():(output/name).write_text(json.dumps(data,ensure_ascii=False,allow_nan=False,separators=(',',':')),encoding='utf-8')
 report=result['validation'];lines=['# Validação da base histórica','',f"Fonte: `{report['source_file']}`",f"SHA-256: `{report['source_sha256']}`",'',f"Conferências: {report['summary']['checks']}; correspondências: {report['summary']['matched']}; divergências: {report['summary']['divergences']}; indisponíveis: {report['summary']['unavailable']}.",'','| Aba | Linhas lidas | Registros aceitos |','|---|---:|---:|']
 for group,config in GROUPS.items():lines.append(f"| {config['sheet']} | {report['input_rows'][group]} | {report['accepted_records'][group]} |")
 lines+=['','## Ocorrências','']+[json.dumps(issue,ensure_ascii=False) for issue in report['issues']]
 for check in report['checks']:
  if check['status']!='match':lines.append(json.dumps(check,ensure_ascii=False))
 if not report['issues'] and not report['summary']['divergences']:lines.append('Nenhuma duplicação, inconsistência de datas ou divergência de totais identificada.')
 lines+=['','## Limites de interpretação','']+['- '+v for v in result['manifest']['methodology']]
 (output.parent/'VALIDACAO.md').write_text('\n'.join(lines)+'\n',encoding='utf-8')

def main(argv=None):
 parser=argparse.ArgumentParser(description=__doc__)
 parser.add_argument('--input',type=Path,required=True)
 parser.add_argument('--output',type=Path,default=ROOT/'historico/data')
 args=parser.parse_args(argv)
 result=import_workbook(args.input);write_outputs(result,args.output)
 print(json.dumps({'records':result['manifest']['record_counts'],'validation':result['validation']['summary'],'issues':len(result['validation']['issues'])},ensure_ascii=False))
 return 2 if result['validation']['summary']['divergences'] or result['validation']['quarantined_items'] else 0

if __name__=='__main__':raise SystemExit(main())
