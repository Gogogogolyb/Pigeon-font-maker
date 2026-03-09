import os
import uuid
import json
import subprocess
import shutil
from flask import Flask, request, send_file, jsonify
from werkzeug.utils import secure_filename
from PIL import Image

app = Flask(__name__)
app.config['UPLOAD_FOLDER'] = 'uploads'
app.config['MAX_CONTENT_LENGTH'] = 16 * 1024 * 1024
os.makedirs(app.config['UPLOAD_FOLDER'], exist_ok=True)

def allowed_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in {'png', 'jpg', 'jpeg', 'bmp'}

def binarize_image(pil_img, threshold=128):
    gray = pil_img.convert('L')
    bw = gray.point(lambda x: 0 if x < threshold else 255, '1')
    return bw

def pil_to_bmp(pil_img, path):
    pil_img.save(path, 'BMP')

def run_potrace(bmp_path, svg_path, turdsize=2):
    try:
        # Добавляем параметр turdsize (удаление шумов)
        subprocess.run(['potrace', '-s', '-t', str(turdsize), '-o', svg_path, bmp_path], 
                       check=True, capture_output=True)
        return True
    except subprocess.CalledProcessError as e:
        print(f"Potrace error: {e.stderr.decode()}")
        return False

def create_fontforge_script(glyphs_svg, output_ttf, fontname, out_w, out_h, monospaced):
    """
    Генерирует скрипт для FontForge с масштабированием глифов под заданный em-квадрат.
    out_w, out_h — желаемые размеры глифа в пикселях (не в юнитах), но для простоты
    будем считать, что em = 1000, и масштабируем так, чтобы глиф занимал 80% от em,
    с центрированием.
    """
    em = 1000  # стандартный em-квадрат
    target_size = min(out_w, out_h) * 0.8  # 80% от меньшей стороны

    script_lines = [
        "import fontforge",
        f"font = fontforge.font()",
        f"font.fontname = '{fontname}'",
        f"font.familyname = '{fontname}'",
        f"font.fullname = '{fontname}'",
        f"font.encoding = 'UnicodeFull'",
        "font.em = 1000",
    ]

    for g in glyphs_svg:
        char = g['char']
        svg_path = g['svg_path'].replace('\\', '/')
        code = ord(char)
        script_lines.extend([
            f"glyph = font.createChar({code}, '{char}')",
            f"glyph.importOutlines('{svg_path}')",
            "bbox = glyph.boundingBox()",
            # bbox = (xmin, ymin, xmax, ymax)
            "width = bbox[2] - bbox[0]",
            "height = bbox[3] - bbox[1]",
            f"scale = {target_size} / max(width, height) if max(width, height) != 0 else 1",
            # Масштабируем и центрируем
            "glyph.transform([scale, 0, 0, scale, (1000 - width*scale)/2 - bbox[0]*scale, (1000 - height*scale)/2 - bbox[1]*scale])",
            "glyph.width = 1000",  # стандартная ширина
        ])

    # Если моноширинный, делаем все глифы одинаковой ширины
    if monospaced == 'fixed':
        script_lines.append("# Make all glyphs monospaced")
        script_lines.append("max_width = 1000")
        script_lines.append("for glyph in font.glyphs():")
        script_lines.append("    if glyph.width > max_width: max_width = glyph.width")
        script_lines.append("for glyph in font.glyphs():")
        script_lines.append("    glyph.width = max_width")

    script_lines.append(f"font.generate('{output_ttf}')")
    return "\n".join(script_lines)

def run_fontforge_script(script_content, work_dir):
    script_path = os.path.join(work_dir, 'build.py')
    with open(script_path, 'w') as f:
        f.write(script_content)
    try:
        subprocess.run(['fontforge', '-script', script_path], cwd=work_dir, check=True, capture_output=True)
        return True
    except subprocess.CalledProcessError as e:
        print(f"FontForge error: {e.stderr.decode()}")
        return False

@app.route('/convert', methods=['POST'])
def convert():
    # 1. Получаем файл и параметры
    if 'sprite' not in request.files:
        return jsonify({'error': 'No file uploaded'}), 400
    file = request.files['sprite']
    if file.filename == '':
        return jsonify({'error': 'Empty filename'}), 400

    # Параметры
    chars_str = request.form.get('chars', '[]')
    rects_json = request.form.get('rects', '{}')
    fontname = request.form.get('fontname', 'CustomFont')
    threshold = int(request.form.get('threshold', 128))
    turdsize = int(request.form.get('turdsize', 2))
    monospaced = request.form.get('monospaced', 'auto')
    out_w = int(request.form.get('outWidth', 64))
    out_h = int(request.form.get('outHeight', 64))

    try:
        rects = json.loads(rects_json)
        chars = json.loads(chars_str)  # массив символов (для проверки)
    except:
        return jsonify({'error': 'Invalid JSON data'}), 400

    # 2. Сохраняем изображение
    ext = file.filename.rsplit('.', 1)[1].lower()
    filename = secure_filename(f"{uuid.uuid4().hex}.{ext}")
    filepath = os.path.join(app.config['UPLOAD_FOLDER'], filename)
    file.save(filepath)

    # 3. Создаём рабочую папку
    job_id = uuid.uuid4().hex
    job_dir = os.path.join(app.config['UPLOAD_FOLDER'], job_id)
    os.makedirs(job_dir, exist_ok=True)

    # 4. Открываем изображение
    try:
        sprite = Image.open(filepath).convert('RGBA')
    except Exception as e:
        shutil.rmtree(job_dir)  # чистим за собой
        return jsonify({'error': f'Cannot open image: {e}'}), 400

    glyphs_svg = []
    # 5. Для каждого символа вырезаем, бинаризуем, трассируем
    for ch, rect in rects.items():
        x, y, w, h = rect['x'], rect['y'], rect['width'], rect['height']
        char_img = sprite.crop((x, y, x+w, y+h))
        bw = binarize_image(char_img, threshold=threshold)
        bmp_path = os.path.join(job_dir, f"{ord(ch)}.bmp")
        pil_to_bmp(bw, bmp_path)
        svg_path = os.path.join(job_dir, f"{ord(ch)}.svg")
        if not run_potrace(bmp_path, svg_path, turdsize):
            continue
        glyphs_svg.append({'char': ch, 'svg_path': svg_path})

    if not glyphs_svg:
        shutil.rmtree(job_dir)
        return jsonify({'error': 'No glyphs could be traced'}), 400

    # 6. Генерируем скрипт и TTF
    ttf_filename = f"{fontname}.ttf"
    ttf_path = os.path.join(job_dir, ttf_filename)
    script = create_fontforge_script(glyphs_svg, ttf_path, fontname, out_w, out_h, monospaced)
    if not run_fontforge_script(script, job_dir):
        shutil.rmtree(job_dir)
        return jsonify({'error': 'Font generation failed'}), 500

    # 7. Отправляем файл
    response = send_file(ttf_path, as_attachment=True, download_name=ttf_filename)
    # После отправки можно запланировать удаление временных файлов (через after_this_request)
    @response.call_on_close
    def cleanup():
        try:
            shutil.rmtree(job_dir)
            os.remove(filepath)
        except:
            pass
    return response

@app.route('/')
def index():
    return send_file('index.html')

if __name__ == '__main__':
    app.run(debug=True, host='0.0.0.0', port=5000)
