from pathlib import Path
p = Path('orchestrator_v2.py')
c = p.read_text(encoding='utf-8')
c = c.replace('parent.parent / "desktop_modules" / "form_fill.py"', 'parent / "form_fill.py"')
p.write_text(c, encoding='utf-8')
print('Path fixed')
