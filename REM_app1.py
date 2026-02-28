from flask import Flask, redirect, url_for, render_template, request, session, flash, jsonify
from datetime import timedelta
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy.sql import func
from werkzeug.security import generate_password_hash, check_password_hash
from flask_mail import Mail, Message
import random
from werkzeug.utils import secure_filename
import os
from flask import g
import string
import json
from dotenv import load_dotenv

load_dotenv()

APP_SECRET_KEY= os.getenv("APP_SECRET_KEY") #26
EMAIL_ID=os.getenv("EMAIL_ID") #39
EMAIL_KEY=os.getenv("EMAIL_KEY") #40




app = Flask(__name__)

app.secret_key = APP_SECRET_KEY
app.config['SQLALCHEMY_DATABASE_URI']='sqlite:///usersdb.sqlite3'
app.config['SQLALCHEMY_BINDS'] = {
    'rem_btn_nxt_db': 'sqlite:///rem_btn_nxt_db.sqlite3',
    'rem_users_db': 'sqlite:///rem_users_db.sqlite3',
    'transacs_db': 'sqlite:///transacsdb.sqlite3'
}
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
db = SQLAlchemy(app)

# Email configuration
app.config['MAIL_SERVER'] = 'smtp.googlemail.com'
app.config['MAIL_PORT'] = 587
app.config['MAIL_USERNAME'] = EMAIL_ID
app.config['MAIL_PASSWORD'] = EMAIL_KEY
app.config['MAIL_USE_TLS'] = True
app.config['MAIL_USE_SSL'] = False

mail = Mail(app)
temp_username=""
temp_pass=""
global icon
icon = "&#x2705;"



class User(db.Model):
    __bind_key__ = 'rem_users_db'
    id = db.Column("id", db.Integer, primary_key=True)
    username = db.Column("username", db.String(100), nullable=False, unique=True)
    build_no=db.Column("build_no", db.Integer,nullable=True)#remove true later and perform a amanual check, keep + manual check
    password = db.Column("password", db.String(100), nullable=False)
class REM(db.Model):
    __bind_key__ = 'rem_btn_nxt_db'
    id = db.Column("id", db.Integer, primary_key=True)
    build_no = db.Column("build_no", db.Integer,nullable=True)#
    btn1_json = db.Column("btn1_json",db.Text, nullable=True)#prev. false
    btn2_json = db.Column("btn2_json",db.Text, nullable=True)
    btn3_json = db.Column("btn3_json",db.Text, nullable=True)
    btn4_json = db.Column("btn4_json",db.Text, nullable=True)
    nxt_sig = db.Column("next_sig", db.String(100), nullable=True)

    def set_btn(self,btn_no, numbers_list):
        print(f'entered set_btn\n{btn_no}, {numbers_list}\n')
        if btn_no==1:
            self.btn1_json = json.dumps(numbers_list)
        elif btn_no==2:
            self.btn2_json = json.dumps(numbers_list)
        elif btn_no==3:
            self.btn3_json = json.dumps(numbers_list)
        elif btn_no==4:
            self.btn4_json = json.dumps(numbers_list)
        else:
            print(f'set_btn func. failed due to btn_no mismatch\n')

    def get_btn(self, btn_no):
        if btn_no==1:
            return json.loads(self.btn1_json)
        elif btn_no==2:
            return json.loads(self.btn2_json)
        elif btn_no==3:
            return json.loads(self.btn3_json)
        elif btn_no==4:
            return json.loads(self.btn4_json)
        else:
            print(f'get_btn func. failed due to btn_no mismatch, sending None\n')
        return None#this could cause error
class Transac(db.Model):
    __bind_key__ = 'transacs_db'
    id=db.Column("id", db.Integer, primary_key=True)
    prod_id=db.Column("prod_id", db.Integer, nullable=False, unique=False)
    seller_id=db.Column("seller_id", db.Integer, nullable=False)
    buyer_id=db.Column("buyer_id", db.Integer, nullable=False)
    prod_price=db.Column("prod_price", db.Float, nullable=False)
    date=db.Column("date", db.String, nullable=False)
    time=db.Column("time", db.String, nullable=False)
    location=db.Column("location", db.String(50), nullable=False)
    seller_conf=db.Column("seller_conf", db.Integer, nullable=False)
    buyer_conf=db.Column("buyer_conf", db.Integer, nullable=False)
    completion=db.Column("completion", db.Integer, nullable=False)

@app.route("/", methods=['GET', 'POST'])
def home():
    username = session.get('username')

    # row_rem = REM(build_no=77)
    # db.session.add(row_rem)
    # db.session.commit()

    # row_rem = REM(build_no=78)
    # db.session.add(row_rem)
    # db.session.commit()

    return render_template('REM_home.html', username=username)
@app.route("/login", methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        username = request.form['username']
        password = request.form['password']

        user = User.query.filter_by(username=username).first()

        if user and check_password_hash(user.password, password):
            #flash('Login successful!', 'success')
            session['username'] = username
            session['user_id'] = user.id
            # return redirect(url_for('new_dashboard'))
            # earlier: return redirect(url_for('dashboard', username=username))
            return redirect(url_for('home'))
        else:
            flash('Invalid credentials', 'error')

    return render_template("REM_login.html")

@app.route('/signup', methods=['GET', 'POST'])
def signup():
    if request.method == 'POST':
        username = request.form['username']
        password = request.form['password']
        conf_password = request.form['conf_password']

        if not username or not password:
            flash('Username and password required')
            return redirect(url_for('signup'))

        # if not check_email_domain(username):
        #     flash('Enter your college email ID')
        #     return redirect(url_for('signup'))

        existing_user = User.query.filter_by(username=username).first()
        if existing_user:
            flash('Username already exists', 'error')
        elif password!=conf_password:
            flash('Both the passwords need to be the same', 'error')
        else:
            hashed_pass = generate_password_hash(password)
            global temp_username
            global temp_pass
            temp_username=username
            temp_pass=hashed_pass

            new_user = User(username=username, password=hashed_pass)
            db.session.add(new_user)
            db.session.commit()
            session['username'] = username
            # temp_row=User.query.filter_by(username=session['username']).first()
            # session['user_id'] = temp_row.id
            return redirect(url_for('home'))
    return render_template('REM_signup.html')
@app.route("/logout")
def logout():
    session.clear()

    flash('Logged out successfully!')
    # g.user=None
    return redirect(url_for('home'))
@app.route('/link', methods=['GET', 'POST'])
def link():
    #if not logged in dont open tyhis page
    if 'username' not in session:
        return redirect(url_for('home'))
    if request.method == 'POST':
        build_no = request.form['link']


        if not build_no:
            flash('build number required')
            return redirect(url_for('link'))

        user_with_entered_build_no=User.query.filter_by(build_no=build_no).first()#finds a user with entered build_no

        if user_with_entered_build_no:#if user with entered build_no found
            flash('build number already linked to another user', 'error')
        else:
            userr=User.query.filter_by(username=session['username']).first()
            userr.build_no = build_no
            db.session.add(userr)
            db.session.commit()


            return redirect(url_for('home'))
    build_no=User.query.filter_by(username=session['username']).first().build_no
    return render_template("REM_link.html", build_no=build_no)
@app.route('/setup', methods=['GET', 'POST'])
def setup():
    return render_template('REM_setup.html')
@app.route('/receive/<int:btn>', methods=['GET', 'POST'])
def receive(btn):
    row_user=User.query.filter_by(username=session.get('username')).first()
    user_build_no=row_user.build_no
    row_rem=REM.query.filter_by(build_no=user_build_no).first()
    if btn==1:
        row_rem.nxt_sig='1_recv'#msg for esp32, esp32 must send a delete request after reading this data
    elif btn==2:
        row_rem.nxt_sig='2_recv'#msg for esp32, esp32 must send a delete request after reading this data
    elif btn==3:
        row_rem.nxt_sig='3_recv'#msg for esp32, esp32 must send a delete request after reading this data
    elif btn==4:
        row_rem.nxt_sig='4_recv'#msg for esp32, esp32 must send a delete request after reading this data
    else:
        print(f'next_sig remains as prevoius as no match found\n')
    db.session.add(row_rem)
    db.session.commit()
    return redirect(url_for('setup'))


@app.route('/wahid_login', methods=['POST', 'GET'])
def wahidlogin():
    return render_template('wahidlogin.html')

@app.route('/esp_check_nxt_sig/<str>', methods=['GET', 'POST'])
def esp_check_nxt_sig(str):#str is build_no sent by esp32
    row_rem=REM.query.filter_by(build_no=int(str)).first()
    nxt_sig=row_rem.nxt_sig
    row_rem.nxt_sig=None
    db.session.add(row_rem)
    db.session.commit()
    return nxt_sig

@app.route('/esp_sends_ir_data', methods=['GET', 'POST'])#post only bt chatgpt
def esp_sends_ir_data():
    ir_data=request.get_json()
    print(f'{ir_data}\n')
    if not ir_data or 'raw' not in ir_data:
        return jsonify({"status":"error", "message":"Missing raw data"}),400
    raw_list=ir_data['raw']

    btn_no=ir_data['btn_no']
    print(f'{btn_no}, {raw_list}\n')

    #row_user=User.query.filter_by(username=session.get('username')).first()
    #user_build_no=row_user.build_no
    user_build_no=78
    row_rem=REM.query.filter_by(build_no=user_build_no).first()
    row_rem.set_btn(btn_no,raw_list)
    db.session.commit()
    print(f"received {len(raw_list)} micro values.\n")
    return jsonify({"status":"success", "received":len(ir_data)}),200

@app.route('/open_remote', methods=['GET', 'POST'])
def open_remote():
    return render_template('REM_open_remote.html')
@app.route('/send/<int:btn>', methods=['GET', 'POST'])
def send(btn):
    row_user=User.query.filter_by(username=session.get('username')).first()
    user_build_no=row_user.build_no
    row_rem=REM.query.filter_by(build_no=user_build_no).first()
    if btn==1:
        row_rem.nxt_sig='1_send'#msg for esp32, esp32 must send a delete request after reading this data
    elif btn==2:
        row_rem.nxt_sig='2_send'#msg for esp32, esp32 must send a delete request after reading this data
    elif btn==3:
        row_rem.nxt_sig='3_send'#msg for esp32, esp32 must send a delete request after reading this data
    elif btn==4:
        row_rem.nxt_sig='4_send'#msg for esp32, esp32 must send a delete request after reading this data
    else:
        print(f'next_sig remains as prevoius as no match found\n')
    db.session.add(row_rem)
    db.session.commit()
    return redirect(url_for('open_remote'))
@app.route('/esp_req_ir_data/<int:btn>', methods=['GET', 'POST'])
def esp_req_ir_data(btn):
    row_rem=REM.query.filter_by(build_no=78).first()
    if btn==1:
        return jsonify(row_rem.btn1_json)#btn1_json is a python list
    elif btn==2:
        return jsonify(row_rem.btn2_json)
    elif btn==3:
        return jsonify(row_rem.btn3_json)
    elif btn==4:
        return jsonify(row_rem.btn4_json)
    else:
        return None
if __name__ == "__main__":
    with app.app_context():
        db.create_all()
    app.run(debug=False)
