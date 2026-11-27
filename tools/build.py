"""Render a portable HTML from the dashboard template and the checked-in inputs."""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def render():
    html = (ROOT / "index_template.html").read_text()
    data = (ROOT / "data.json").read_text().strip().replace("</", "<\\/")
    html = html.replace("/*__DATA__*/", data)
    for name in ("access.js", "model.js"):
        content = (ROOT / name).read_text().replace("</script", "<\\/script")
        html = html.replace(f'<script src="{name}"></script>', f'<script>\n{content}\n</script>')
    (ROOT / "index.html").write_text(html)


if __name__ == "__main__":
    render()
