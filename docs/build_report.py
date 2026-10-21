"""Render the versioned Markdown report using ReportLab and local figures.

Run with the bundled Python runtime. No network or patient-data inputs.
"""
from pathlib import Path
import html
import re
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.lib import colors
from reportlab.lib.styles import ParagraphStyle
from reportlab.platypus import (SimpleDocTemplate, Paragraph, Spacer, PageBreak,
                               KeepTogether, Image, Table, TableStyle)

SRC = Path(__file__).resolve().parent / "jag8_track2_report.md"
DEST = SRC.with_suffix(".pdf")
FONTDIR = Path('/System/Library/Fonts/Supplemental')
for name, filename in [('Body','Georgia.ttf'), ('Body-Bold','Georgia Bold.ttf'),
                       ('Body-Italic','Georgia Italic.ttf'), ('Body-BoldItalic','Georgia Bold Italic.ttf'),
                       ('Sans','Arial.ttf'), ('Sans-Bold','Arial Bold.ttf'),
                       ('Sans-Italic','Arial Italic.ttf'), ('Sans-BoldItalic','Arial Bold Italic.ttf')]:
    pdfmetrics.registerFont(TTFont(name, str(FONTDIR / filename)))
pdfmetrics.registerFontFamily('Body', normal='Body', bold='Body-Bold', italic='Body-Italic', boldItalic='Body-BoldItalic')
pdfmetrics.registerFontFamily('Sans', normal='Sans', bold='Sans-Bold', italic='Sans-Italic', boldItalic='Sans-BoldItalic')
NAVY = colors.HexColor('#14305a')
WIDTH = 504
ST = {
 'body': ParagraphStyle('body', fontName='Body', fontSize=10, leading=13.5, spaceAfter=7, textColor='#1b2433'),
 'h1': ParagraphStyle('h1', fontName='Sans-Bold', fontSize=25, leading=28, spaceAfter=7, textColor=NAVY),
 'subtitle': ParagraphStyle('subtitle', fontName='Sans-Bold', fontSize=12.5, leading=16, spaceAfter=12, textColor=NAVY),
 'h2': ParagraphStyle('h2', fontName='Sans-Bold', fontSize=14, leading=17, spaceBefore=10, spaceAfter=8, keepWithNext=True, textColor=NAVY),
 'h3': ParagraphStyle('h3', fontName='Sans-Bold', fontSize=11, leading=14, spaceBefore=8, spaceAfter=6, keepWithNext=True, textColor=NAVY),
 'cell': ParagraphStyle('cell', fontName='Sans', fontSize=8.2, leading=10.6, textColor='#1b2433'),
 'th': ParagraphStyle('th', fontName='Sans-Bold', fontSize=8.2, leading=10.6, textColor=NAVY),
 'caption': ParagraphStyle('caption', fontName='Body-Italic', fontSize=8.7, leading=11.5, spaceBefore=6, spaceAfter=9, textColor='#3b4c66'),
 'ref': ParagraphStyle('ref', fontName='Sans', fontSize=8.5, leading=11.4, spaceAfter=4, textColor='#1b2433'),
 'quote': ParagraphStyle('quote', fontName='Sans-Bold', fontSize=10, leading=13.5, borderPadding=7, backColor='#e8f6f5', spaceAfter=10),
}

def inline(s):
    s = html.escape(s)
    s = re.sub(r'\[([^]]+)\]\((https?://[^\s)]+)\)', r'<link href="\2" color="#14305a">\1</link>', s)
    s = re.sub(r'\*\*(.+?)\*\*', r'<b>\1</b>', s)
    s = re.sub(r'\*(.+?)\*', r'<i>\1</i>', s)
    s = s.replace('→', '<font name="Sans">→</font>')
    return s

def footer(c, doc):
    c.saveState()
    c.setStrokeColor(colors.HexColor('#d5dfe9'))
    c.line(54, 39, 558, 39)
    c.setFont('Sans', 8)
    c.setFillColor(NAVY)
    c.drawString(54, 26, 'jag8  |  MVA Hackathon 2026 - Track 2')
    c.drawRightString(558, 26, str(doc.page))
    c.restoreState()

def main():
    lines = SRC.read_text().splitlines()
    story = []
    i = 0
    table_index = 0
    references = False
    while i < len(lines):
        line = lines[i].strip()
        if not line:
            i += 1
            continue
        if line.startswith('!['):
            path = re.search(r'\]\((.+)\)', line).group(1)
            fig1 = 'overview' in path
            if fig1:
                story.append(PageBreak())
            im = Image(str(SRC.parent / path))
            maxw, maxh = (460, 605) if fig1 else (504, 310)
            scale = min(maxw / im.imageWidth, maxh / im.imageHeight)
            im.drawWidth, im.drawHeight = im.imageWidth * scale, im.imageHeight * scale
            i += 1
            while i < len(lines) and not lines[i].strip(): i += 1
            caption = lines[i].strip().strip('*')
            story.append(KeepTogether([im, Paragraph(inline(caption), ST['caption'])]))
            i += 1
            continue
        if line.startswith('# '):
            story.append(Paragraph(inline(line[2:]), ST['h1']))
        elif line.startswith('## '):
            title = line[3:]
            if re.match(r'[2345679]\. ', title): story.append(PageBreak())
            if title.startswith('11. '): references = True
            style = 'h2' if re.match(r'\d+\. ', title) else 'subtitle'
            story.append(Paragraph(inline(title), ST[style]))
        elif line.startswith('### '):
            title = line[4:]
            if title == 'Exposure plan': story.append(PageBreak())
            story.append(Paragraph(inline(title), ST['h3']))
        elif line.startswith('|'):
            rows = []
            while i < len(lines) and lines[i].strip().startswith('|'):
                cells = [x.strip() for x in lines[i].strip().strip('|').split('|')]
                if not all(re.fullmatch(r':?-+:?', c) for c in cells): rows.append(cells)
                i += 1
            n = len(rows[0])
            # Column proportions tuned to the actual reviewer-facing tables.
            proportions = {4: ([.18,.28,.32,.22] if 'Priority' in rows[0][0] else [.14,.29,.29,.28]),
                           3: ([.16,.47,.37] if rows[0][0] == 'Gate' else [.18,.46,.36]),
                           2: ([.35,.65])}[n]
            if rows[0][0] == 'Official criterion': proportions = [.22,.10,.68]
            data = [[Paragraph(inline(x), ST['th' if j == 0 else 'cell']) for x in row] for j,row in enumerate(rows)]
            t = Table(data, colWidths=[WIDTH*x for x in proportions], repeatRows=1, hAlign='LEFT')
            t.setStyle(TableStyle([('GRID',(0,0),(-1,-1),.45,colors.HexColor('#c9d3e0')),
                                   ('BACKGROUND',(0,0),(-1,0),colors.HexColor('#eef3f9')),
                                   ('VALIGN',(0,0),(-1,-1),'TOP'),
                                   ('LEFTPADDING',(0,0),(-1,-1),5),('RIGHTPADDING',(0,0),(-1,-1),5),
                                   ('TOPPADDING',(0,0),(-1,-1),3 if rows[0][0] == 'Official criterion' else 5),('BOTTOMPADDING',(0,0),(-1,-1),3 if rows[0][0] == 'Official criterion' else 5)]))
            story += [KeepTogether([t]), Spacer(1,8)]
            table_index += 1
            continue
        elif line.startswith('> '):
            story.append(Paragraph(inline(line[2:]), ST['quote']))
        else:
            parts = [line]
            while i+1 < len(lines) and lines[i+1].strip() and not re.match(r'^(#|\||!\[|>|\d+\. )', lines[i+1].strip()):
                i += 1
                parts.append(lines[i].strip())
            story.append(Paragraph(inline(' '.join(parts)), ST['ref' if references else 'body']))
        i += 1
    SimpleDocTemplate(str(DEST), pagesize=(612,792), leftMargin=54, rightMargin=54,
                      topMargin=43, bottomMargin=51,
                      title='Real rescue, not false rescue - Track 2',
                      author='jag8').build(story, onFirstPage=footer, onLaterPages=footer)
    print('Wrote', DEST)

if __name__ == '__main__': main()
