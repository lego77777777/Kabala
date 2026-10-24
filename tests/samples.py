"""
Labeled sample emails for detection tests. Written by hand to resemble real Hebrew and English
receipts and the kinds of mail that look like them (promos, shipping, quotes, carts).
They are synthetic: real-world accuracy must be re-measured on a real, anonymized mailbox sample.
"""

from kabala.detect.message import Attachment, ParsedEmail

# (email, is_receipt, expected_company or None, expected_amount or None, expected_currency or None)
SAMPLES: list[tuple[ParsedEmail, bool, str | None, float | None, str | None]] = []


def _add(subject, sender, text, is_receipt, company=None, amount=None, currency=None, pdf=None, date="2026-10-14"):
    name, _, email = sender.partition("|")
    attachments = [Attachment(pdf, "application/pdf", "att1", 20000)] if pdf else []
    msg = ParsedEmail(
        id=f"m{len(SAMPLES)}", thread_id="t", message_id=f"<m{len(SAMPLES)}@test>", subject=subject,
        sender_name=name, sender_email=email, date=date, text=text, html=None, attachments=attachments,
    )
    SAMPLES.append((msg, is_receipt, company, amount, currency))


# ---- receipts -----------------------------------------------------------------

_add("הקבלה שלך מ-Netflix", "Netflix|info@account.netflix.com",
     "שלום,\nתודה שאתה חבר ב-Netflix.\nתוכנית: Standard\nסה\"כ: ₪54.90\nאמצעי תשלום: Visa •••• 1234",
     True, "Netflix", 54.90, "ILS")
_add("Your Spotify Premium receipt", "Spotify|no-reply@spotify.com",
     "Thanks for your payment.\nPremium Individual  1 month\nTotal ₪23.90\nPaid with Mastercard ending 5678",
     True, "Spotify", 23.90, "ILS")
_add("חשבונית חשמל לתקופה 07/2026", "חברת החשמל לישראל|noreply@iec.co.il",
     "לקוח יקר,\nמצורפת חשבונית החשמל שלך.\nצריכה: 512 קוט\"ש\nסכום לתשלום: 412.37 ש\"ח\nהתשלום יחויב בהוראת קבע.",
     True, "חברת החשמל", 412.37, "ILS", pdf="IEC_invoice_0726.pdf")
_add("חשבונית מס מבזק לחודש יולי", "בזק|invoice@bezeq.co.il",
     "חשבונית מס מספר 4455667\nשירותי אינטרנט  99.00\nשירותי טלפון  30.00\nסה״כ לתשלום 129.00 ₪",
     True, "בזק", 129.00, "ILS")
_add("Your Amazon.com order of \"USB-C Cable...\"", "Amazon.com|auto-confirm@amazon.com",
     "Thank you for your order.\nOrder #112-1234567-1234567\nItem(s) Subtotal: $45.98\nShipping & Handling: $3.22\nOrder Total: $49.20",
     True, "Amazon", 49.20, "USD")
_add("Your receipt from Apple.", "Apple|no_reply@email.apple.com",
     "Receipt\nDocument No. 186732554\niCloud+ 200 GB  Monthly\nTOTAL ₪39.90",
     True, "Apple", 39.90, "ILS")
_add("הקבלה שלך מ-Wolt", "Wolt|info@wolt.com",
     "תודה על ההזמנה!\nמסעדה: שיפודי הדר\nמנות  128.50\nדמי משלוח  14.00\nסה\"כ ₪ 142.50",
     True, "Wolt", 142.50, "ILS")
_add("Google One: הקבלה שלך", "Google Payments|payments-noreply@google.com",
     "Google One 100 GB\nTotal: ₪7.90\nPayment method: Visa •••• 1234",
     True, "Google", 7.90, "ILS")
_add("Your Adobe invoice is ready", "Adobe|mail@mail.adobe.com",
     "Your monthly invoice for Adobe Photography Plan is attached.\nAmount paid US$22.99",
     True, "Adobe", 22.99, "USD", pdf="Invoice_IE12345678.pdf")
_add("Receipt for your payment to Valve Corporation", "PayPal|service@paypal.com",
     "You sent a payment of $19.99 USD to Valve Corporation\nTransaction ID: 8AB12345CD678901E\nTotal $19.99 USD",
     True, None, 19.99, "USD")
_add("אישור תשלום - סופר-פארם", "סופר-פארם|noreply@super-pharm.co.il",
     "תודה שקנית בסופר-פארם אונליין\nהזמנה מספר 9988776\nסכום החיוב: 87.60 ש״ח",
     True, "סופר-פארם", 87.60, "ILS")
_add("חשבונית חודשית - פרטנר", "Partner|billing@partner.co.il",
     "החשבונית החודשית שלך מוכנה\nחבילת סלולר 59.00\nסה\"כ לתשלום כולל מע\"מ 59.00 ₪",
     True, "Partner", 59.00, "ILS", pdf="partner_invoice.pdf")
_add("[GitHub] Payment Receipt for octocat", "GitHub|noreply@github.com",
     "We received payment for your GitHub Pro subscription.\nAmount: $4.00 USD\nCharged to: Visa ending 4242",
     True, "GitHub", 4.00, "USD")
_add("Your Monday evening trip with Uber", "Uber Receipts|noreply@uber.com",
     "Here's your receipt for your ride.\nTrip fare ₪52.20\nBooking fee ₪6.00\nTotal ₪58.20",
     True, "Uber", 58.20, "ILS")
_add("Order 8170012345 – payment successful", "AliExpress|transaction@notice.aliexpress.com",
     "Your payment was successful.\nOrder total: US $12.34",
     True, "AliExpress", 12.34, "USD")
_add("קבלה על הזמנה מתן ביס", "תן ביס|noreply@10bis.co.il",
     "בתאבון!\nהזמנה ממסעדת בורגר סאלון\nסה\"כ: 65 ₪\nחויב בכרטיס תן ביס",
     True, "תן ביס", 65.0, "ILS")
_add("Thank you for your Steam purchase!", "Steam Store|noreply@steampowered.com",
     "Thank you for your recent transaction on Steam.\nHollow Knight  $14.99\nTotal: $14.99",
     True, "Steam", 14.99, "USD")
_add("אישור תשלום ארנונה", "עיריית תל אביב-יפו|noreply@tel-aviv.gov.il",
     "התקבל תשלום עבור ארנונה לתקופה 09-10/2026\nסכום ששולם: 1,234.00 ש\"ח\nמספר אסמכתא 55443322",
     True, None, 1234.00, "ILS")
_add("Your Dropbox payment", "Dropbox|no-reply@dropbox.com",
     "Thanks for being a Dropbox Plus customer. Your payment has been processed.\n$11.99 has been charged to your card.",
     True, "Dropbox", 11.99, "USD", pdf="Dropbox_receipt_Oct2026.pdf")
_add("תודה שקניתם ב-KSP", "KSP|orders@ksp.co.il",
     "קבלה מס' 123456\nאוזניות אלחוטיות  349.00\nסה\"כ 349 ₪\nשולם בכרטיס אשראי",
     True, "KSP", 349.0, "ILS")

# ---- not receipts ---------------------------------------------------------------

_add("20% הנחה על ההזמנה הבאה שלך", "Wolt|promo@wolt.com",
     "מתגעגעים! קבלו קופון WOLT20 להזמנה הבאה. המבצע בתוקף השבוע בלבד.", False)
_add("Your package has shipped", "Amazon.com|shipment-tracking@amazon.com",
     "Your package is on its way.\nTracking number: TBA123456789\nArriving Thursday", False)
_add("Black Friday deals – save up to 50%", "Shop|news@shop.example.com",
     "Our biggest sale of the year. Headphones from $9.99. Limited time only! Unsubscribe", False)
_add("Reset your Apple ID password", "Apple|appleid@id.apple.com",
     "Someone requested a password reset for your Apple ID. If this was you, click the link below.", False)
_add("הצעת מחיר לשיפוץ המטבח", "יוסי שיפוצים|yossi@renovations.co.il",
     "שלום, מצורפת הצעת מחיר כפי שסיכמנו.\nעבודה וחומרים\nסה\"כ: 12,000 ₪\nההצעה בתוקף ל-30 יום.", False)
_add("Join our webinar: Tax tips for freelancers", "Accounting Pro|events@accountingpro.com",
     "Register now for our free webinar next Tuesday.", False)
_add("You left something in your cart", "Shop|cart@shop.example.com",
     "Your items are waiting.\nWireless mouse $54.00\nComplete your purchase before it's gone.", False)
_add("פלייליסט חדש בשבילך", "Spotify|no-reply@spotify.com",
     "יצרנו לך פלייליסט חדש על בסיס מה שאהבת השבוע.", False)
_add("Meeting: budget review", "Dana|dana@company.com",
     "Hi, can we go over the Q4 invoices on Thursday at 10?", False)
_add("החבילה שלך בדרך אליך", "AliExpress|notice@aliexpress.com",
     "החבילה נשלחה ותגיע בקרוב. מספר מעקב: LP00123456789", False)
_add("New sign-in to your Netflix account", "Netflix|info@account.netflix.com",
     "We noticed a new sign-in to your Netflix account from a Windows device.", False)
_add("עדכון מוצרים חדשים בחנות", "סופר-פארם|news@super-pharm.co.il",
     "מבצעים חמים השבוע! 1+1 על כל מוצרי הטיפוח. הצטרפו למועדון.", False)
