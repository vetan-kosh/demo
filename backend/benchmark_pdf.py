"""Repeatable local Form-16 renderer benchmark (no customer data required)."""
import argparse, os, statistics, tempfile, time
from types import SimpleNamespace
from pdf_generator import generate_form16_pdf

def sample():
    employee=SimpleNamespace(name='Benchmark User',designation='Teacher',office_school_name='Benchmark School',pan='ABCDE1234F',father_name='Benchmark',district_name='Ranchi')
    employer=SimpleNamespace(employer_name='Benchmark School',employer_address='Ranchi, Jharkhand',pan='AAAAA1111A',tan='RANC12345E',officer_name='Benchmark DDO',officer_father_name='Benchmark',designation='DDO',district_name='Ranchi')
    months=[(m,2025 if m>=3 else 2026) for m in [3,4,5,6,7,8,9,10,11,12,1,2]]
    ledger=[dict(month=m,year=y,month_year=f'{m:02d}-{y}',financial_year='2025-26',basic_pay=30000,da=15000,hra=3000,gross_salary=50000,line_items={},line_items_json={},deductions={'gpf':3000,'income_tax_tds':1000},deductions_json={'gpf':3000,'income_tax_tds':1000},source='benchmark',is_auto_generated=False,note='',flags=[]) for m,y in months]
    return employee,employer,ledger

def main():
    ap=argparse.ArgumentParser(); ap.add_argument('-n','--runs',type=int,default=10); args=ap.parse_args()
    employee,employer,ledger=sample(); times=[]
    with tempfile.TemporaryDirectory(prefix='vetankosh-bench-') as d:
        for i in range(args.runs):
            out=os.path.join(d,f'form16-{i}.pdf'); t=time.perf_counter(); generate_form16_pdf(employee,employer,ledger,'2025-26',out); elapsed=time.perf_counter()-t; times.append(elapsed); print(f'run {i+1}: {elapsed:.3f}s ({os.path.getsize(out)} bytes)')
    warm=times[1:] or times
    print(f'average: {statistics.mean(times):.3f}s')
    print(f'median: {statistics.median(times):.3f}s')
    print(f'warm average: {statistics.mean(warm):.3f}s')
    print(f'warm theoretical PDFs/hour: {3600/statistics.mean(warm):.0f}')
if __name__=='__main__': main()
