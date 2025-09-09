from flask import Blueprint, jsonify, Response
import csv
import io

from services.db import get_conversation

export_bp = Blueprint('export', __name__)

@export_bp.route('/export/conversation/<numero>')
def export_conversation_json(numero):
    data = get_conversation(numero)
    return jsonify(data)

@export_bp.route('/export/conversation/<numero>.csv')
def export_conversation_csv(numero):
    data = get_conversation(numero)
    output = io.StringIO()
    writer = csv.writer(output)
    headers = list(data.keys())
    writer.writerow(headers)
    writer.writerow([data[h] for h in headers])
    response = Response(output.getvalue(), mimetype='text/csv')
    response.headers['Content-Disposition'] = f'attachment; filename=conversation_{numero}.csv'
    return response
