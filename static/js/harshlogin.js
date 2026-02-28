const passwordInput = document.getElementById('password');
const passwordStrength = document.getElementById('password-strength');
const errorMessage = document.getElementById('error-message');

passwordInput.addEventListener('input', () => {
    const password = passwordInput.value;
    const strength = checkPasswordStrength(password);
    passwordStrength.textContent = strength;
});

function checkPasswordStrength(password) {
    
    if (password.length < 8) {
        return "Weak";
    } else if (password.length < 12) {
        return "Medium";
    } else {
        return "Strong";
    }
}


const form = document.querySelector('form');
form.addEventListener('submit', (event) => {
    event.preventDefault(); 

    
    if (passwordInput.value.length < 8) {
        errorMessage.textContent = "Password must be at least 8 characters long.";
        errorMessage.style.display = 'block';
    } else {
       
        errorMessage.style.display = 'none'; 
        alert("Form submitted!"); 
    }
});