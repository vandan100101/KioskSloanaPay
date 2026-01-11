"""
Helmet Sanitizer Kiosk - PayMongo QRPh Integration (COMPLETE FIXED VERSION)
"""

from flask import Flask, render_template, jsonify, url_for, request, redirect, session
import requests
import os
import qrcode
import base64
import time
import sqlite3
import json
import re
from io import BytesIO
from datetime import datetime, timedelta
from functools import wraps
from urllib.parse import urlencode

# --- Raspberry Pi GPIO Setup ---
try:
    import RPi.GPIO as GPIO
    RPI_AVAILABLE = True
    print("✅ GPIO Module Loaded - Running on Raspberry Pi")
except (ImportError, RuntimeError):
    RPI_AVAILABLE = False
    print("⚠️ GPIO Not Available - Running in Simulation Mode")

# --- Flask App Configuration ---
app = Flask(__name__)
app.config['SECRET_KEY'] = os.getenv("SECRET_KEY", "change-this-secret-key-in-production")

# --- PayMongo Configuration ---
PAYMONGO_SECRET_KEY = os.getenv("PAYMONGO_SECRET_KEY", "sk_live_bM912rC4nCCyYNyToVkb3qfv")
PAYMONGO_PUBLIC_KEY = os.getenv("PAYMONGO_PUBLIC_KEY", "pk_live_K85mpvqom3eJtEDsDWJcziTA")
PAYMONGO_WEBHOOK_SECRET = os.getenv("PAYMONGO_WEBHOOK_SECRET", "")
PAYMONGO_API_URL = "https://api.paymongo.com/v1"

# Solana Pay Configuration
SOLANA_RECIPIENT_ADDRESS = os.getenv("SOLANA_RECIPIENT_ADDRESS", "YOUR_SOLANA_WALLET_ADDRESS")
SOLANA_AMOUNT = float(os.getenv("SOLANA_AMOUNT", "0"))
SOLANA_LABEL = "Helmet Sanitizer"
SOLANA_MESSAGE = "Payment for helmet sanitization service"
SOLANA_NETWORK = os.getenv("SOLANA_NETWORK", "devnet")

# Admin Configuration
ADMIN_USERNAME = os.getenv("ADMIN_USERNAME", "admin")
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "admin123")

# Payment Amount
PAYMENT_AMOUNT = 1.00  # PHP (changed from 3.00 to match your webhook)

# GPIO Configuration
SANITIZER_PIN = 18
if RPI_AVAILABLE:
    GPIO.setmode(GPIO.BCM)
    GPIO.setup(SANITIZER_PIN, GPIO.OUT, initial=GPIO.LOW)

# In-memory payment tracking
payments = {}

# ========================================
# DATABASE FUNCTIONS
# ========================================

def init_db():
    """Initialize SQLite database."""
    conn = sqlite3.connect('helmet_sanitizer.db')
    c = conn.cursor()
    
    c.execute('''
        CREATE TABLE IF NOT EXISTS payments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            reference TEXT UNIQUE NOT NULL,
            payment_method TEXT NOT NULL,
            amount REAL NOT NULL,
            currency TEXT DEFAULT 'PHP',
            status TEXT NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            paid_at TIMESTAMP,
            paymongo_id TEXT,
            qr_code TEXT,
            reference_id TEXT
        )
    ''')
    
    c.execute('''
        CREATE TABLE IF NOT EXISTS sanitization_sessions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            payment_id INTEGER,
            started_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            completed_at TIMESTAMP,
            duration INTEGER DEFAULT 10,
            FOREIGN KEY (payment_id) REFERENCES payments (id)
        )
    ''')
    
    c.execute('''
        CREATE TABLE IF NOT EXISTS ratings (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id INTEGER,
            rating INTEGER NOT NULL CHECK(rating >= 1 AND rating <= 5),
            feedback TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (session_id) REFERENCES sanitization_sessions (id)
        )
    ''')
    
    c.execute('''
        CREATE TABLE IF NOT EXISTS daily_stats (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            date DATE UNIQUE NOT NULL,
            total_payments INTEGER DEFAULT 0,
            total_revenue REAL DEFAULT 0,
            successful_sanitizations INTEGER DEFAULT 0,
            average_rating REAL DEFAULT 0,
            qrph_payments INTEGER DEFAULT 0,
            cash_payments INTEGER DEFAULT 0
        )
    ''')
    
    conn.commit()
    conn.close()
    print("✅ Database Initialized")


def get_db():
    """Get database connection."""
    conn = sqlite3.connect('helmet_sanitizer.db')
    conn.row_factory = sqlite3.Row
    return conn


def save_payment(reference, method, amount, status='PENDING', paymongo_id=None, qr_code=None, reference_id=None):
    """Save payment to database."""
    conn = get_db()
    c = conn.cursor()
    try:
        c.execute('''
            INSERT INTO payments (reference, payment_method, amount, status, paymongo_id, qr_code, reference_id)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        ''', (reference, method, amount, status, paymongo_id, qr_code, reference_id))
        conn.commit()
        payment_id = c.lastrowid
        conn.close()
        return payment_id
    except sqlite3.IntegrityError:
        conn.close()
        return None


def update_payment_status(reference, status, paymongo_payment_id=None):
    """Update payment status."""
    conn = get_db()
    c = conn.cursor()
    paid_at = datetime.now() if status == 'PAID' else None
    
    if paymongo_payment_id:
        c.execute('''
            UPDATE payments 
            SET status = ?, paid_at = ?, paymongo_id = ?
            WHERE reference = ?
        ''', (status, paid_at, paymongo_payment_id, reference))
    else:
        c.execute('''
            UPDATE payments 
            SET status = ?, paid_at = ?
            WHERE reference = ?
        ''', (status, paid_at, reference))
    
    conn.commit()
    conn.close()


def get_payment_by_reference(reference):
    """Get payment by reference."""
    conn = get_db()
    c = conn.cursor()
    c.execute('SELECT * FROM payments WHERE reference = ?', (reference,))
    payment = c.fetchone()
    conn.close()
    return dict(payment) if payment else None


def save_sanitization_session(payment_id):
    """Create sanitization session."""
    conn = get_db()
    c = conn.cursor()
    c.execute('''
        INSERT INTO sanitization_sessions (payment_id, started_at)
        VALUES (?, ?)
    ''', (payment_id, datetime.now()))
    session_id = c.lastrowid
    conn.commit()
    conn.close()
    return session_id


def complete_sanitization_session(session_id):
    """Mark sanitization as complete."""
    conn = get_db()
    c = conn.cursor()
    c.execute('''
        UPDATE sanitization_sessions 
        SET completed_at = ?
        WHERE id = ?
    ''', (datetime.now(), session_id))
    conn.commit()
    conn.close()


def save_rating(session_id, rating, feedback=None):
    """Save rating."""
    conn = get_db()
    c = conn.cursor()
    c.execute('''
        INSERT INTO ratings (session_id, rating, feedback)
        VALUES (?, ?, ?)
    ''', (session_id, rating, feedback))
    conn.commit()
    conn.close()


def update_daily_stats():
    """Update daily statistics."""
    conn = get_db()
    c = conn.cursor()
    today = datetime.now().date()
    
    c.execute('''
        SELECT 
            COUNT(*) as total_payments,
            COALESCE(SUM(amount), 0) as total_revenue,
            SUM(CASE WHEN payment_method = 'QRPH' THEN 1 ELSE 0 END) as qrph_payments,
            SUM(CASE WHEN payment_method = 'CASH' THEN 1 ELSE 0 END) as cash_payments
        FROM payments 
        WHERE DATE(created_at) = ? AND status = 'PAID'
    ''', (today,))
    payment_stats = c.fetchone()
    
    c.execute('''
        SELECT COUNT(*) as successful_sanitizations
        FROM sanitization_sessions
        WHERE DATE(started_at) = ? AND completed_at IS NOT NULL
    ''', (today,))
    sanitization_stats = c.fetchone()
    
    c.execute('''
        SELECT COALESCE(AVG(rating), 0) as average_rating
        FROM ratings
        WHERE DATE(created_at) = ?
    ''', (today,))
    rating_stats = c.fetchone()
    
    c.execute('''
        INSERT INTO daily_stats 
        (date, total_payments, total_revenue, successful_sanitizations, average_rating, qrph_payments, cash_payments)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(date) DO UPDATE SET
            total_payments = excluded.total_payments,
            total_revenue = excluded.total_revenue,
            successful_sanitizations = excluded.successful_sanitizations,
            average_rating = excluded.average_rating,
            qrph_payments = excluded.qrph_payments,
            cash_payments = excluded.cash_payments
    ''', (
        today,
        payment_stats[0] or 0,
        payment_stats[1] or 0,
        sanitization_stats[0] or 0,
        rating_stats[0] or 0,
        payment_stats[2] or 0,
        payment_stats[3] or 0
    ))
    
    conn.commit()
    conn.close()


init_db()

# ========================================
# HELPER FUNCTIONS
# ========================================

def trigger_sanitizer():
    """Activate sanitizer relay."""
    if not RPI_AVAILABLE:
        print("💡 [SIMULATION] Sanitizer running for 10 seconds...")
        time.sleep(10)
        print("✅ [SIMULATION] Sanitizer complete")
        return
    
    print(f"✅ Sanitizer ON (GPIO Pin {SANITIZER_PIN})")
    GPIO.output(SANITIZER_PIN, GPIO.HIGH)
    time.sleep(10)
    GPIO.output(SANITIZER_PIN, GPIO.LOW)
    print("🧼 Sanitizer OFF")


def login_required(f):
    """Admin login decorator."""
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if 'admin_logged_in' not in session:
            return redirect(url_for('admin_login'))
        return f(*args, **kwargs)
    return decorated_function


def create_paymongo_headers():
    """Create authenticated headers for PayMongo API."""
    auth_string = f"{PAYMONGO_SECRET_KEY}:"
    auth_b64 = base64.b64encode(auth_string.encode('utf-8')).decode('utf-8')
    
    return {
        "accept": "application/json",
        "Content-Type": "application/json",
        "authorization": f"Basic {auth_b64}"
    }

# ========================================
# KIOSK ROUTES
# ========================================

@app.route("/")
def home():
    """Main kiosk screen."""
    return render_template("index.html")


@app.route("/pay/qr")
def qr_payment():
    """QRPh payment screen."""
    return render_template("qr_payment.html")


@app.route("/solana_pay")
def solana_pay():
    """Render Solana Pay payment page."""
    return render_template("solana_payment.html")


@app.route("/pay/cash")
def cash_payment():
    """Cash payment screen."""
    return render_template("cash_payment.html")


@app.route("/rating/<session_id>")
def rating_page(session_id):
    """Rating page."""
    return render_template("rating.html", session_id=session_id)


@app.route("/debug")
@login_required
def debug_page():
    """Webhook debugger page."""
    return render_template("debug.html")

# ========================================
# PAYMONGO QRPh PAYMENT
# ========================================

@app.route("/create_payment", methods=["POST"])
def create_payment():
    """Create PayMongo QRPh payment."""
    reference = f"helmet-{int(time.time())}-{os.urandom(3).hex()}"
    amount = PAYMENT_AMOUNT
    
    try:
        headers = create_paymongo_headers()
        
        # Create QRPh payment
        payload = {
            "data": {
                "attributes": {
                    "kind": "instore",
                    "amount": int(amount * 100),
                    "currency": "PHP",
                    "reference_number": reference,
                    "description": f"Helmet Sanitization - Ref: {reference}",
                    "metadata": {
                        "reference_number": reference,
                        "product": "helmet_sanitization",
                        "kiosk_id": "helmet_kiosk_001"
                    }
                }
            }
        }
        
        print(f"🔵 Creating PayMongo QRPh for ₱{amount}")
        print(f"   Reference: {reference}")
        
        # Call the QRPh endpoint
        response = requests.post(
            f"{PAYMONGO_API_URL}/qrph/generate",
            json=payload,
            headers=headers,
            timeout=10
        )
        
        print(f"   Response Status: {response.status_code}")
        
        if response.status_code not in [200, 201]:
            print(f"❌ PayMongo Error: {response.text}")
            return jsonify({
                "error": "Payment gateway error", 
                "details": response.text
            }), 400
        
        response_data = response.json()
        
        # Extract QR code from response
        qrph_data = response_data.get('data', {})
        qrph_id = qrph_data.get('id')
        attributes = qrph_data.get('attributes', {})
        
        # PayMongo returns the QR code as base64 PNG
        qr_image_data = attributes.get('qr_image')
        reference_id = attributes.get('reference_id')
        
        if not qr_image_data:
            print(f"❌ No QR code image in response")
            return jsonify({"error": "No QR code received"}), 400
        
        print(f"   QRPh ID: {qrph_id}")
        print(f"   Reference ID: {reference_id}")
        
        # Extract base64 data
        if 'base64,' in qr_image_data:
            qr_b64 = qr_image_data.split('base64,')[1]
        else:
            qr_b64 = qr_image_data
        
        # Save to database
        payment_id = save_payment(
            reference=reference,
            method='QRPH',
            amount=amount,
            status='PENDING',
            paymongo_id=qrph_id,
            qr_code=reference_id,
            reference_id=reference_id
        )
        
        # Store in memory
        payments[reference] = {
            "id": payment_id,
            "status": "PENDING",
            "method": "QRPH",
            "paymongo_id": qrph_id,
            "reference_id": reference_id
        }
        
        print(f"✅ PayMongo QRPh Payment Created Successfully")
        
        return jsonify({
            "success": True,
            "reference": reference,
            "qr_image": qr_b64,
            "amount": f"₱{amount:.2f}",
            "reference_id": reference_id,
            "gateway": "PayMongo QRPh",
            "qrph_id": qrph_id
        })
    
    except requests.exceptions.RequestException as e:
        print(f"❌ Network Error: {e}")
        return jsonify({"error": "Network error", "details": str(e)}), 500
    
    except Exception as e:
        print(f"❌ Error creating payment: {e}")
        import traceback
        traceback.print_exc()
        return jsonify({"error": "Server error", "details": str(e)}), 500


@app.route("/check_payment/<ref>", methods=["GET"])
def check_payment(ref):
    """Check payment status."""
    print(f"🔍 Checking payment: {ref}")
    
    # Check database first
    payment = get_payment_by_reference(ref)
    if not payment:
        return jsonify({"status": "NOT_FOUND"}), 404
    
    # If already paid, return immediately
    if payment["status"] == "PAID":
        # Get session ID if exists
        conn = get_db()
        c = conn.cursor()
        c.execute('SELECT id FROM sanitization_sessions WHERE payment_id = ? ORDER BY id DESC LIMIT 1', (payment["id"],))
        session = c.fetchone()
        conn.close()
        
        session_id = session[0] if session else payment.get("id")
        return jsonify({"status": "PAID", "session_id": session_id})
    
    # For testing/demo: Allow manual marking as paid via query parameter
    if request.args.get('test') == 'true':
        print(f"🧪 Test mode: Manually marking {ref} as PAID")
        return mark_payment_as_paid(ref, payment["id"])
    
    return jsonify({"status": "PENDING"})


def mark_payment_as_paid(ref, payment_db_id):
    """Mark payment as paid and trigger sanitizer."""
    # Update payment status
    update_payment_status(ref, "PAID")
    
    # Create sanitization session
    session_id = save_sanitization_session(payment_db_id)
    
    # Update memory
    if ref in payments:
        payments[ref]["status"] = "PAID"
        payments[ref]["session_id"] = session_id
    
    print(f"🧼 Triggering sanitizer for session {session_id}")
    
    # Trigger sanitizer
    trigger_sanitizer()
    
    # Complete sanitization
    complete_sanitization_session(session_id)
    
    # Update stats
    update_daily_stats()
    
    print(f"✅ Payment {ref} completed successfully!")
    
    return jsonify({"status": "PAID", "session_id": session_id})

# ========================================
# PAYMONGO WEBHOOK (FIXED VERSION)
# ========================================

@app.route("/paymongo_webhook", methods=["POST"])
def paymongo_webhook():
    """
    PayMongo webhook endpoint - FIXED VERSION
    """
    print("\n" + "="*60)
    print("📩 PAYMONGO WEBHOOK RECEIVED")
    print("="*60)
    
    try:
        # Get raw data and try to parse it
        raw_data = request.get_data(as_text=True)
        print(f"📦 Raw data length: {len(raw_data)} bytes")
        
        # Try to parse as JSON
        try:
            data = json.loads(raw_data)
            print(f"📊 Parsed JSON successfully")
            print(f"📋 Data keys: {list(data.keys())}")
            
            # Log the full data structure for debugging
            print("\n📋 WEBHOOK DATA STRUCTURE:")
            print(json.dumps(data, indent=2)[:1000])  # First 1000 chars
            
        except json.JSONDecodeError as e:
            print(f"❌ JSON parsing error: {e}")
            print(f"📄 Raw data preview: {raw_data[:500]}")
            return jsonify({"received": True}), 200
        
        # Handle different webhook formats
        event_type = data.get("type") or data.get("event")
        
        # Also check nested event type
        if not event_type and data.get("data") and isinstance(data["data"], dict):
            nested_attrs = data["data"].get("attributes", {})
            event_type = nested_attrs.get("type")
        
        print(f"🎯 Event Type: {event_type}")
        
        # Handle payment.paid event
        if event_type in ["payment.paid", "payment.success", "payment_success"]:
            print("💰 PAYMENT PAID EVENT DETECTED")
            return process_webhook_payment(data)
        
        elif event_type in ["payment.failed", "payment.failure"]:
            print("❌ PAYMENT FAILED EVENT")
            return jsonify({"received": True}), 200
        
        elif event_type in ["qrpayment.expired", "qr.expired"]:
            print("⏰ QR PAYMENT EXPIRED EVENT")
            return jsonify({"received": True}), 200
        
        else:
            print(f"⚠️ Unhandled event type: {event_type}")
            # Try to process anyway in case it's a payment
            return process_webhook_payment(data)
    
    except Exception as e:
        print(f"❌ Webhook processing error: {str(e)}")
        import traceback
        traceback.print_exc()
        return jsonify({"received": True}), 200


def process_webhook_payment(data):
    """Process payment webhook data - FIXED VERSION."""
    try:
        # Try different possible data structures
        payment_data = None
        event_data = None
        
        # NEW: Check if this is an event wrapper (payment.paid structure)
        if data.get("data") and isinstance(data["data"], dict):
            # This is the event attributes level
            event_attrs = data["data"].get("attributes", {})
            
            # The actual payment is inside event_attrs.data
            if event_attrs.get("data") and isinstance(event_attrs["data"], dict):
                event_data = event_attrs["data"]
                payment_data = event_data.get("attributes", {})
                print(f"📦 Found payment in event wrapper structure")
            
            # Fallback: direct attributes
            elif data["data"].get("attributes"):
                payment_data = data["data"]["attributes"]
        
        # Structure 2: direct attributes
        elif data.get("attributes"):
            payment_data = data["attributes"]
        
        if not payment_data:
            print("❌ Could not extract payment data from webhook")
            print(f"🔍 Data structure: {json.dumps(data, indent=2)[:800]}")
            return jsonify({"received": True}), 200
        
        print(f"📋 Payment data keys: {list(payment_data.keys())}")
        
        # Extract amount and status
        amount = payment_data.get("amount", 0)
        if isinstance(amount, int):
            amount = amount / 100  # Convert centavos to PHP
        
        status = payment_data.get("status", "")
        description = payment_data.get("description", "")
        
        print(f"💰 Amount: ₱{amount:.2f}")
        print(f"📊 Status: {status}")
        print(f"📝 Description: {description}")
        
        # Get the payment ID from the event data
        payment_id_from_webhook = None
        if event_data:
            payment_id_from_webhook = event_data.get("id")
            print(f"🆔 Payment ID: {payment_id_from_webhook}")
        
        # NEW: Try to get the QRPh ID from the source
        source = payment_data.get("source", {})
        qrph_id = None
        if isinstance(source, dict):
            qrph_id = source.get("id")
            print(f"🔍 QRPh ID from source: {qrph_id}")
        
        # Strategy 1: Get reference from metadata
        metadata = payment_data.get("metadata") or {}
        reference = metadata.get("reference_number") if isinstance(metadata, dict) else None
        if reference:
            print(f"✅ Reference from metadata: {reference}")
        
        # Strategy 2: Get from billing
        if not reference:
            billing = payment_data.get("billing") or {}
            reference = billing.get("reference_number") or billing.get("reference") if isinstance(billing, dict) else None
            if reference:
                print(f"✅ Reference from billing: {reference}")
        
        # Strategy 3: Extract from description (QR id pattern)
        if not reference and description:
            # Look for our reference pattern first
            match = re.search(r'helmet-\d+-[a-f0-9]+', description)
            if match:
                reference = match.group(0)
                print(f"✅ Found reference in description: {reference}")
        
        # Strategy 4: NEW - Look up by QRPh ID in database
        if not reference and qrph_id:
            print(f"🔍 Searching database for QRPh ID: {qrph_id}")
            try:
                conn = get_db()
                c = conn.cursor()
                c.execute('SELECT reference FROM payments WHERE paymongo_id = ?', (qrph_id,))
                result = c.fetchone()
                conn.close()
                
                if result:
                    reference = result[0]
                    print(f"✅ Found reference via QRPh ID: {reference}")
            except Exception as e:
                print(f"⚠️ Error searching by QRPh ID: {e}")
        
        # Strategy 5: NEW - Look up by reference_id or external_reference_number
        if not reference:
            external_ref = payment_data.get("external_reference_number") or payment_data.get("reference_id")
            if external_ref:
                print(f"🔍 Searching database for external reference: {external_ref}")
                try:
                    conn = get_db()
                    c = conn.cursor()
                    c.execute('SELECT reference FROM payments WHERE reference_id = ?', (external_ref,))
                    result = c.fetchone()
                    conn.close()
                    
                    if result:
                        reference = result[0]
                        print(f"✅ Found reference via external reference: {reference}")
                except Exception as e:
                    print(f"⚠️ Error searching by external ref: {e}")
        
        # Strategy 6: LAST RESORT - Search for most recent pending payment with matching amount
        if not reference and amount > 0:
            print(f"🔍 Last resort: searching for pending payment with amount ₱{amount:.2f}")
            try:
                conn = get_db()
                c = conn.cursor()
                c.execute('''
                    SELECT reference FROM payments 
                    WHERE status = 'PENDING' 
                    AND payment_method = 'QRPH'
                    AND amount = ?
                    ORDER BY created_at DESC 
                    LIMIT 1
                ''', (amount,))
                result = c.fetchone()
                conn.close()
                
                if result:
                    reference = result[0]
                    print(f"⚠️ Matched by amount to pending payment: {reference}")
            except Exception as e:
                print(f"⚠️ Error searching by amount: {e}")
        
        if not reference:
            print("❌ No reference found in webhook data after all strategies")
            print(f"📦 Full payment data:")
            print(json.dumps(payment_data, indent=2)[:1000])
            return jsonify({"received": True, "error": "No reference found"}), 200
        
        print(f"🎯 Processing payment for reference: {reference}")
        
        # Find payment in database
        payment = get_payment_by_reference(reference)
        if not payment:
            print(f"⚠️ Payment not found in database: {reference}")
            # Create new payment record
            payment_id = save_payment(
                reference=reference,
                method='QRPH',
                amount=amount,
                status='PAID',
                paymongo_id=payment_id_from_webhook or qrph_id
            )
            if payment_id:
                payment = {"id": payment_id, "status": "PENDING"}
                print(f"✅ Created new payment record: {reference}")
            else:
                return jsonify({"received": True, "error": "Failed to create payment"}), 200
        
        # Skip if already paid
        if payment["status"] == "PAID":
            print(f"✅ Payment already marked as PAID: {reference}")
            return jsonify({"received": True, "already_paid": True}), 200
        
        print(f"💳 Updating payment status to PAID...")
        
        # Update payment status
        update_payment_status(reference, "PAID", payment_id_from_webhook or qrph_id)
        
        # Create sanitization session
        session_id = save_sanitization_session(payment["id"])
        
        # Update memory
        if reference in payments:
            payments[reference]["status"] = "PAID"
            payments[reference]["session_id"] = session_id
        else:
            payments[reference] = {
                "id": payment["id"],
                "status": "PAID",
                "session_id": session_id,
                "method": "QRPH"
            }
        
        print(f"🧼 Triggering sanitizer for session {session_id}")
        
        # Trigger sanitizer
        trigger_sanitizer()
        
        # Complete sanitization
        complete_sanitization_session(session_id)
        
        # Update stats
        update_daily_stats()
        
        print(f"✅ Payment {reference} processed successfully via webhook!")
        print(f"✅ Session {session_id} completed!")
        
        return jsonify({
            "success": True,
            "message": "Payment processed",
            "reference": reference,
            "session_id": session_id
        }), 200
    
    except Exception as e:
        print(f"❌ Error processing webhook payment: {str(e)}")
        import traceback
        traceback.print_exc()
        return jsonify({"received": True}), 200

# ========================================
# CASH PAYMENT
# ========================================

@app.route("/simulate_cash", methods=["POST"])
def simulate_cash():
    """Simulate cash payment."""
    reference = f"helmet-cash-{int(time.time())}-{os.urandom(3).hex()}"
    amount = PAYMENT_AMOUNT
    
    payment_id = save_payment(reference, 'CASH', amount, 'PAID')
    session_id = save_sanitization_session(payment_id)
    
    print(f"💵 Cash Payment: {reference}")
    
    trigger_sanitizer()
    complete_sanitization_session(session_id)
    update_daily_stats()
    
    return jsonify({
        "status": "PAID",
        "message": "Cash received",
        "session_id": session_id
    })

# ========================================
# SOLANA PAY ROUTES
# ========================================

@app.route("/create_solana_payment", methods=["POST"])
def create_solana_payment():
    """Create Solana Pay payment request."""
    try:
        reference = f"helmet-sol-{int(time.time())}-{os.urandom(3).hex()}"
        
        # Save to database
        payment_id = save_payment(reference, 'SOLANA', SOLANA_AMOUNT, 'PENDING')
        
        # Store in memory
        payments[reference] = {
            "id": payment_id,
            "status": "PENDING",
            "method": "SOLANA"
        }
        
        # Build Solana Pay URL
        params = {
            'recipient': SOLANA_RECIPIENT_ADDRESS,
            'amount': str(SOLANA_AMOUNT),
            'label': SOLANA_LABEL,
            'message': SOLANA_MESSAGE,
            'reference': reference,
            'memo': f"Helmet-{reference}"
        }
        
        # Use urlencode to properly encode the parameters
        solana_url = f"solana:{SOLANA_RECIPIENT_ADDRESS}?{urlencode(params)}"
        
        # Generate QR code
        qr = qrcode.QRCode(version=1, box_size=10, border=4)
        qr.add_data(solana_url)
        qr.make(fit=True)
        
        img = qr.make_image(fill_color="black", back_color="white")
        buffer = BytesIO()
        img.save(buffer, format="PNG")
        qr_b64 = base64.b64encode(buffer.getvalue()).decode("utf-8")
        
        print(f"✅ Solana Payment Created: {reference}")
        
        return jsonify({
            "success": True,
            "reference": reference,
            "qr_image": qr_b64,
            "amount": f"{SOLANA_AMOUNT} SOL",
            "solana_url": solana_url,
            "recipient": SOLANA_RECIPIENT_ADDRESS,
            "network": SOLANA_NETWORK
        })
    
    except Exception as e:
        print(f"❌ Error creating Solana payment: {str(e)}")
        import traceback
        traceback.print_exc()
        return jsonify({
            "error": "Failed to create Solana payment",
            "details": str(e)
        }), 500


@app.route("/check_solana_payment/<ref>", methods=["GET"])
def check_solana_payment(ref):
    """Check Solana payment status."""
    try:
        payment = get_payment_by_reference(ref)
        if payment:
            return jsonify({
                "success": True,
                "status": payment["status"],
                "reference": ref,
                "amount": payment["amount"],
                "method": payment["payment_method"]
            })
        return jsonify({
            "success": False,
            "error": "Payment not found",
            "status": "NOT_FOUND"
        }), 404
    except Exception as e:
        print(f"❌ Error checking Solana payment: {str(e)}")
        return jsonify({
            "success": False,
            "error": "Server error",
            "details": str(e)
        }), 500


@app.route("/confirm_solana_payment", methods=["POST"])
def confirm_solana_payment():
    """Confirm Solana payment (simulated for testing)."""
    try:
        data = request.get_json()
        if not data:
            return jsonify({"error": "No data provided"}), 400
            
        reference = data.get("reference")
        signature = data.get("signature", "simulated-signature")
        
        if not reference:
            return jsonify({"error": "Missing reference"}), 400
        
        # Update payment status
        update_payment_status(reference, "PAID", signature)
        
        # Update memory
        if reference in payments:
            payments[reference]["status"] = "PAID"
            
            # Create sanitization session
            payment = get_payment_by_reference(reference)
            if payment:
                session_id = save_sanitization_session(payment["id"])
                payments[reference]["session_id"] = session_id
        
        # Trigger sanitizer
        trigger_sanitizer()
        
        # Complete sanitization
        if reference in payments and "session_id" in payments[reference]:
            complete_sanitization_session(payments[reference]["session_id"])
        
        # Update stats
        update_daily_stats()
        
        print(f"🟣 Solana Payment Confirmed: {reference}")
        
        return jsonify({
            "success": True,
            "status": "PAID",
            "message": "Payment confirmed",
            "session_id": payments[reference].get("session_id") if reference in payments else None
        })
    
    except Exception as e:
        print(f"❌ Error confirming Solana payment: {str(e)}")
        return jsonify({
            "success": False,
            "error": "Failed to confirm payment",
            "details": str(e)
        }), 500

# ========================================
# RATING SYSTEM
# ========================================

@app.route("/submit_rating", methods=["POST"])
def submit_rating():
    """Submit rating."""
    data = request.get_json()
    session_id = data.get("session_id")
    rating = data.get("rating")
    feedback = data.get("feedback", "")
    
    if not session_id or not rating:
        return jsonify({"error": "Missing data"}), 400
    
    try:
        rating = int(rating)
        if rating < 1 or rating > 5:
            return jsonify({"error": "Invalid rating"}), 400
        
        save_rating(session_id, rating, feedback)
        update_daily_stats()
        
        print(f"⭐ Rating: {rating} stars (Session: {session_id})")
        return jsonify({"success": True, "message": "Thank you!"})
    
    except Exception as e:
        print(f"❌ Rating error: {e}")
        return jsonify({"error": "Failed to save"}), 500

# ========================================
# TESTING & DEBUGGING ENDPOINTS
# ========================================

@app.route("/test_payment/<ref>", methods=["GET"])
def test_payment(ref):
    """Test endpoint to mark payment as paid."""
    print(f"🧪 TESTING PAYMENT: {ref}")
    
    payment = get_payment_by_reference(ref)
    if not payment:
        return jsonify({"error": "Payment not found"}), 404
    
    # Update to paid
    update_payment_status(ref, "PAID")
    
    # Create session
    session_id = save_sanitization_session(payment["id"])
    
    # Store session ID
    if ref in payments:
        payments[ref]["status"] = "PAID"
        payments[ref]["session_id"] = session_id
    
    print(f"✅ TEST: Payment {ref} marked as paid. Session: {session_id}")
    
    return jsonify({
        "status": "MANUALLY_PAID",
        "session_id": session_id,
        "message": "Payment manually marked as paid"
    })


@app.route("/mark_paid/<ref>", methods=["POST"])
def mark_paid(ref):
    """Manually mark payment as paid."""
    print(f"✅ Manually marking payment as paid: {ref}")
    
    payment = get_payment_by_reference(ref)
    if not payment:
        return jsonify({"error": "Payment not found"}), 404
    
    # Update payment status
    update_payment_status(ref, "PAID")
    
    # Create sanitization session
    session_id = save_sanitization_session(payment["id"])
    
    # Update memory
    if ref in payments:
        payments[ref]["status"] = "PAID"
        payments[ref]["session_id"] = session_id
    
    # Trigger sanitizer
    trigger_sanitizer()
    
    # Complete sanitization
    complete_sanitization_session(session_id)
    
    # Update stats
    update_daily_stats()
    
    return jsonify({
        "success": True,
        "status": "PAID",
        "session_id": session_id,
        "message": "Payment manually marked as paid"
    })


@app.route("/payment_paid", methods=["POST"])
def payment_paid():
    """Simulate payment paid webhook."""
    print("💰 SIMULATING PAYMENT PAID WEBHOOK")
    
    data = request.get_json()
    if not data:
        return jsonify({"error": "No data"}), 400
    
    reference = data.get("reference")
    if not reference:
        return jsonify({"error": "No reference"}), 400
    
    print(f"🔍 Processing simulated payment for: {reference}")
    
    payment = get_payment_by_reference(reference)
    if not payment:
        return jsonify({"error": "Payment not found"}), 404
    
    # Process as paid
    return mark_payment_as_paid(reference, payment["id"])


@app.route("/webhook_debug", methods=["POST"])
def webhook_debug():
    """Debug webhook endpoint."""
    print("\n🔧 WEBHOOK DEBUG ENDPOINT")
    
    # Log headers
    print("📋 Headers:")
    for key, value in request.headers.items():
        print(f"   {key}: {value}")
    
    # Log raw data
    raw_data = request.get_data(as_text=True)
    print(f"📦 Raw data ({len(raw_data)} bytes):")
    print(raw_data[:500] + "..." if len(raw_data) > 500 else raw_data)
    
    return jsonify({
        "received": True,
        "headers": dict(request.headers),
        "data_preview": raw_data[:500] if raw_data else None
    })

# ========================================
# ADMIN ROUTES
# ========================================

@app.route("/admin/login", methods=["GET", "POST"])
def admin_login():
    """Admin login page."""
    if request.method == "POST":
        username = request.form.get("username")
        password = request.form.get("password")
        
        if username == ADMIN_USERNAME and password == ADMIN_PASSWORD:
            session['admin_logged_in'] = True
            print(f"✅ Admin Login: {username}")
            return redirect(url_for('admin_dashboard'))
        else:
            print(f"❌ Failed Login Attempt: {username}")
            return render_template("admin_login.html", error="Invalid credentials")
    
    return render_template("admin_login.html")


@app.route("/admin/logout")
def admin_logout():
    """Admin logout."""
    session.pop('admin_logged_in', None)
    print("🚪 Admin Logged Out")
    return redirect(url_for('admin_login'))


@app.route("/admin")
@app.route("/admin/dashboard")
@login_required
def admin_dashboard():
    """Admin dashboard with overview."""
    conn = get_db()
    c = conn.cursor()
    
    # Today's stats
    today = datetime.now().date()
    c.execute('SELECT * FROM daily_stats WHERE date = ?', (today,))
    today_stats = c.fetchone()
    
    # This week's stats
    week_ago = today - timedelta(days=7)
    c.execute('''
        SELECT 
            SUM(total_payments) as total_payments,
            SUM(total_revenue) as total_revenue,
            SUM(successful_sanitizations) as successful_sanitizations,
            AVG(average_rating) as average_rating
        FROM daily_stats 
        WHERE date >= ?
    ''', (week_ago,))
    week_stats = c.fetchone()
    
    # All-time stats
    c.execute('''
        SELECT 
            COUNT(*) as total_payments,
            COALESCE(SUM(amount), 0) as total_revenue
        FROM payments 
        WHERE status = 'PAID'
    ''')
    alltime_stats = c.fetchone()
    
    # Recent payments
    c.execute('''
        SELECT * FROM payments 
        ORDER BY created_at DESC 
        LIMIT 20
    ''')
    recent_payments = c.fetchall()
    
    # Recent ratings
    c.execute('''
        SELECT r.*, s.id as session_id, p.reference
        FROM ratings r
        JOIN sanitization_sessions s ON r.session_id = s.id
        JOIN payments p ON s.payment_id = p.id
        ORDER BY r.created_at DESC
        LIMIT 10
    ''')
    recent_ratings = c.fetchall()
    
    conn.close()
    
    return render_template("admin_dashboard.html",
                         today_stats=dict(today_stats) if today_stats else None,
                         week_stats=dict(week_stats) if week_stats else None,
                         alltime_stats=dict(alltime_stats) if alltime_stats else None,
                         recent_payments=[dict(p) for p in recent_payments],
                         recent_ratings=[dict(r) for r in recent_ratings])


@app.route("/admin/payments")
@login_required
def admin_payments():
    """View all payments with filters."""
    conn = get_db()
    c = conn.cursor()
    
    # Get filter parameters
    status = request.args.get('status', 'all')
    method = request.args.get('method', 'all')
    date_from = request.args.get('date_from', '')
    date_to = request.args.get('date_to', '')
    
    # Build query
    query = 'SELECT * FROM payments WHERE 1=1'
    params = []
    
    if status != 'all':
        query += ' AND status = ?'
        params.append(status)
    
    if method != 'all':
        query += ' AND payment_method = ?'
        params.append(method)
    
    if date_from:
        query += ' AND DATE(created_at) >= ?'
        params.append(date_from)
    
    if date_to:
        query += ' AND DATE(created_at) <= ?'
        params.append(date_to)
    
    query += ' ORDER BY created_at DESC LIMIT 100'
    
    c.execute(query, params)
    payments_list = c.fetchall()
    conn.close()
    
    return render_template("admin_payments.html", 
                         payments=[dict(p) for p in payments_list],
                         filters={'status': status, 'method': method, 
                                 'date_from': date_from, 'date_to': date_to})


@app.route("/admin/analytics")
@login_required
def admin_analytics():
    """Detailed analytics page."""
    conn = get_db()
    c = conn.cursor()
    
    # Get date range
    days = int(request.args.get('days', 30))
    start_date = datetime.now().date() - timedelta(days=days)
    
    # Daily stats
    c.execute('''
        SELECT * FROM daily_stats
        WHERE date >= ?
        ORDER BY date
    ''', (start_date,))
    daily_stats = c.fetchall()
    
    # Payment method breakdown
    c.execute('''
        SELECT payment_method, COUNT(*) as count, COALESCE(SUM(amount), 0) as total
        FROM payments
        WHERE status = 'PAID' AND DATE(created_at) >= ?
        GROUP BY payment_method
    ''', (start_date,))
    payment_methods = c.fetchall()
    
    # Rating distribution
    c.execute('''
        SELECT rating, COUNT(*) as count
        FROM ratings
        WHERE DATE(created_at) >= ?
        GROUP BY rating
        ORDER BY rating
    ''', (start_date,))
    rating_distribution = c.fetchall()
    
    # Hourly distribution
    c.execute('''
        SELECT strftime('%H', created_at) as hour, COUNT(*) as count
        FROM payments
        WHERE status = 'PAID' AND DATE(created_at) >= ?
        GROUP BY hour
        ORDER BY hour
    ''', (start_date,))
    hourly_distribution = c.fetchall()
    
    conn.close()
    
    return render_template("admin_analytics.html",
                         daily_stats=[dict(d) for d in daily_stats],
                         payment_methods=[dict(p) for p in payment_methods],
                         rating_distribution=[dict(r) for r in rating_distribution],
                         hourly_distribution=[dict(h) for h in hourly_distribution],
                         days=days)

# ========================================
# UTILITY ENDPOINTS
# ========================================

@app.route("/health")
def health():
    """Health check."""
    return jsonify({
        "status": "OK",
        "gpio_available": RPI_AVAILABLE,
        "database": "connected",
        "payment_gateway": "PayMongo QRPh",
        "webhook_enabled": True,
        "timestamp": datetime.now().isoformat()
    })


@app.route("/webhook_info", methods=["GET"])
def webhook_info():
    """Get webhook information."""
    return jsonify({
        "webhook_url": "https://overgreedy-appealingly-elodia.ngrok-free.dev/paymongo_webhook",
        "status": "active",
        "note": "Configure this URL in PayMongo dashboard webhooks"
    })


@app.route("/list_payments", methods=["GET"])
@login_required
def list_payments():
    """List all payments."""
    conn = get_db()
    c = conn.cursor()
    c.execute('SELECT * FROM payments ORDER BY created_at DESC LIMIT 50')
    payments_list = c.fetchall()
    conn.close()
    
    return jsonify([dict(p) for p in payments_list])

# ========================================
# APP RUNNER
# ========================================

if __name__ == "__main__":
    print("\n" + "="*60)
    print("🚀 HELMET SANITIZER KIOSK - COMPLETE FIXED VERSION")
    print("="*60)
    print(f"GPIO: {'YES ✅' if RPI_AVAILABLE else 'NO ⚠️ (Simulation)'}")
    print(f"Payment Gateway: PayMongo QRPh (GCash & Maya)")
    print(f"Database: helmet_sanitizer.db")
    print(f"Webhook URL: https://overgreedy-appealingly-elodia.ngrok-free.dev/paymongo_webhook")
    print(f"\n📱 Kiosk: http://localhost:5000")
    print(f"🔐 Admin: http://localhost:5000/admin")
    print(f"   Username: {ADMIN_USERNAME}")
    print(f"   Password: {ADMIN_PASSWORD}")
    print(f"🔍 Debug: http://localhost:5000/debug")
    print(f"\n🔧 Test Endpoints:")
    print(f"   Health Check: http://localhost:5000/health")
    print(f"   Webhook Info: http://localhost:5000/webhook_info")
    print(f"   Mark Paid: POST http://localhost:5000/mark_paid/<reference>")
    print("="*60 + "\n")
    
    try:
        app.run(debug=True, host="0.0.0.0", port=5000)
    finally:
        if RPI_AVAILABLE:
            print("\n🧹 Cleaning up GPIO...")
            GPIO.cleanup()
            print("✅ GPIO Cleaned")