from app import app, start_background_workers

start_background_workers()

if __name__ == '__main__':
    app.run(debug=True, host='0.0.0.0', port=5000)
