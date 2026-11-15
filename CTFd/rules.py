from flask import Blueprint, render_template

rules = Blueprint("rules", __name__)

@rules.route("/rules")
def rules_view():
    return render_template("rules.html")