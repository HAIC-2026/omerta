document.addEventListener("DOMContentLoaded", function () {
  var b = document.getElementById("print-btn");
  if (b) b.addEventListener("click", function () { window.print(); });
});
