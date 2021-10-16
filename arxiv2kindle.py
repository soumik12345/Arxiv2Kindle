import os
import re
import sys
import wget
import click
import tarfile
import tempfile
import requests
import subprocess
from glob import glob
import lxml.html as html
from pathlib import Path
from getpass import getpass

import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.application import MIMEApplication


def is_main_file(file_name):
    with open(file_name, 'rt') as f:
        if '\\documentclass' in f.read():
            return True
    return False


def delete_dir(dir_name):
    subprocess.run(['rm', '-rf', dir_name])


def make_single_column(arxiv_dir):
    for filename in Path(arxiv_dir).glob('*.sty'):
        with open(filename, 'rt') as f:
            src = f.readlines()
        out_src = []
        for line in src:
            if line.strip() == '\\twocolumn':
                continue
            out_src.append(line)
        with open(filename, 'wt') as f:
            f.writelines(out_src)


def compile_tex(file_name):
    for _ in range(3):
        subprocess.run(['pdflatex', file_name],
                       stdout=sys.stderr,
                       cwd=file_name.parent)


class Arxiv2KindleConverter:

    def __init__(self, arxiv_url: str, is_landscape: bool) -> None:
        self.arxiv_url = arxiv_url
        self.is_landscape = is_landscape
        self.check_prerequisite()
    
    def check_prerequisite(self):
        result = subprocess.run(["pdflatex", "--version"], stdout=None, stderr=None)
        if result.returncode != 0:
            raise SystemError("no pdflatex found")
        if self.is_landscape:
            result = subprocess.run(["pdftk", "--version"], stdout=None, stderr=None)
            if result.returncode != 0:
                raise SystemError("no pdftk found (required for landscape mode)")
    
    def download_source(self):
        arxiv_id = re.match(r'((http|https)://.*?/)?(?P<id>\d{4}\.\d{4,5}(v\d{1,2})?)', self.arxiv_url).group('id')
        arxiv_abs = f'http://arxiv.org/abs/{arxiv_id}'
        arxiv_pdf = f'http://arxiv.org/pdf/{arxiv_id}'
        arxiv_pgtitle = html.fromstring(
            requests.get(arxiv_abs).text.encode('utf8')).xpath('/html/head/title/text()')[0]
        arxiv_title = re.sub(r'\s+', ' ', re.sub(r'^\[[^]]+\]\s*', '', arxiv_pgtitle), re.DOTALL)
        # create temporary directory
        arxiv_dir = tempfile.mkdtemp(prefix='arxiv2kindle_')
        archive_url = f'http://arxiv.org/e-print/{arxiv_id}'
        # download tar.gz file and add file extension
        tar_filename = wget.download(
            archive_url, out=os.path.join(
                arxiv_dir, ''.join([arxiv_title, '.tar.gz'])))
        if not Path(tar_filename).exists():
            raise SystemError('Paper sources are not available')
        with tarfile.open(tar_filename) as f:
            f.extractall(arxiv_dir)
        return arxiv_dir, arxiv_id, arxiv_title
    
    def process_tex(self, arxiv_dir, geometric_settings):
        texfiles = glob(os.path.join(arxiv_dir, '*.tex'))
        for texfile in texfiles:
            with open(texfile, 'r') as f:
                src = f.readlines()
            if 'documentclass' in src[0]:
                print('correct file: ' + texfile)
                break
        # filter comments/newlines for easier debugging:
        src = [line for line in src if line[0] != '%' and len(line.strip()) > 0]
        # strip font size, column stuff, and paper size stuff in documentclass line:
        src[0] = re.sub(r'\b\d+pt\b', '', src[0])
        src[0] = re.sub(r'\b\w+column\b', '', src[0])
        src[0] = re.sub(r'\b\w+paper\b', '', src[0])
        src[0] = re.sub(r'(?<=\[),', '', src[0]) # remove extraneous starting commas
        src[0] = re.sub(r',(?=[\],])', '', src[0]) # remove extraneous middle/ending commas
        # find begin{document}:
        begindocs = [i for i, line in enumerate(src) if line.startswith(r'\begin{document}')]
        assert(len(begindocs) == 1)
        src.insert(begindocs[0], '\\usepackage['+','.join(
            k+'='+v for k,v in geometric_settings.items())+']{geometry}\n')
        src.insert(begindocs[0], '\\usepackage{times}\n')
        src.insert(begindocs[0], '\\pagestyle{empty}\n')
        if self.is_landscape:
            src.insert(begindocs[0], '\\usepackage{pdflscape}\n')
        for i in range(len(src)):
            line = src[i]
            m = re.search(r'\\includegraphics\[width=([.\d]+)\\(line|text)width\]', line)
            if m:
                mul = m.group(1)
                src[i] = re.sub(
                    r'\\includegraphics\[width=([.\d]+)\\(line|text)width\]',
                    '\\includegraphics[width={mul}\\\\textwidth,height={mul}\\\\textheight,keepaspectratio]'.format(mul=mul),
                    line
                )
        os.rename(texfile, texfile + '.bak')
        with open(texfile, 'w') as f:
            f.writelines(src)
        subprocess.run(
            [
                'pdflatex', texfile,
                '&&', 'pdflatex', texfile,
                '&&', 'pdflatex', texfile
            ], stdout=sys.stderr,
            cwd=Path(texfile).parent
        )
        return texfile[:-4] + '.pdf'
    
    def execute_pipeline(self, width: int, height: int, margin: float):
        arxiv_dir, arxiv_id, arxiv_title = self.download_source()
        print(f'\nArxiv Directory: {arxiv_dir}')
        print(f'Arxiv Title: {arxiv_title}')
        if self.is_landscape:
            width, height = height, width
        geometric_settings = dict(
            paperwidth=f'{width}in',
            paperheight=f'{height}in',
            margin=f'{margin}in'
        )
        try:
            pdf_file = self.process_tex(arxiv_dir, geometric_settings)
            print(f'PDF File: {pdf_file}')
            return pdf_file, arxiv_id, arxiv_title
        except KeyError:
            print('Unable to create pdf file')
            delete_dir(arxiv_dir)
    
    def send_emai(self, pdf_file, arxiv_id, arxiv_title, gmail, kindle_mail):
        msg = MIMEMultipart()
        pdf_part = MIMEApplication(open(pdf_file, 'rb').read(), _subtype='pdf')
        pdf_part.add_header(
            'Content-Disposition', 'attachment',
            filename=arxiv_id+"_" + arxiv_title + ".pdf")
        msg.attach(pdf_part)
        server = smtplib.SMTP('smtp.gmail.com', 587)  
        server.starttls()
        gmail_password = getpass(prompt='Enter Gmail Password: ')
        server.login(gmail, gmail_password)
        server.sendmail(gmail, kindle_mail, msg.as_string())
        server.close()


@click.command()
@click.option('--arxiv_url', '-u', help='Arxiv URL')
@click.option('--width', '-w', default=4, help='Width')
@click.option('--height', '-h', default=6, help='Height')
@click.option('--margin', '-m', default=0.2, help='Margin')
@click.option('--is_landscape', '-l', is_flag=True, help='Flag: Is output landscape')
@click.option('--gmail', '-g', default=None, help='Your Gmail ID')
@click.option('--kindle_mail', '-k', default=None, help='Your Kindle ID')
def main(arxiv_url, width, height, margin, is_landscape, gmail, kindle_mail):
    assert 0. < margin < 1.
    converter = Arxiv2KindleConverter(arxiv_url, is_landscape)
    pdf_file, arxiv_id, arxiv_title = converter.execute_pipeline(width, height, margin)
    if gmail is not None and kindle_mail is not None:
        print('Sending Email...')
        converter.send_emai(pdf_file, arxiv_id, arxiv_title, gmail, kindle_mail)
        print('Done')


if __name__ == '__main__':
    main()
