from flask import Blueprint, jsonify

from services.backup import backup_database

backup_bp = Blueprint('backup', __name__)


@backup_bp.route('/backup', methods=['POST'])
def trigger_backup():
    try:
        backup_path = backup_database()
    except Exception as exc:  # noqa: BLE001 - queremos devolver el error
        return jsonify({'error': str(exc)}), 500

    return jsonify({'backup_path': backup_path})
